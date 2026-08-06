import imaplib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from email import message_from_bytes
from email.header import decode_header, make_header
from email.message import Message
from email.utils import parsedate_to_datetime
from pathlib import Path

from sqlalchemy import and_, delete, func, or_
from sqlmodel import Session, select

from app.config import get_settings
from app.models import (
    BounceRecord,
    Company,
    Contact,
    ContactRoute,
    DraftPrepItem,
    EmailDraft,
    EmailEvent,
    SendRecord,
    SenderAccount,
    Suppression,
)
from app.services.mailer import _read_env_value
from app.services.secrets import decrypt_secret
from app.time_utils import utc_now

REMOVED_EMAIL_DOMAIN = "invalid.invalid"
BOUNCE_FROM_RE = re.compile(r"(mailer-daemon|postmaster|mail delivery|delivery subsystem)", re.IGNORECASE)
BOUNCE_SUBJECT_RE = re.compile(
    r"(undeliver|delivery status notification|delivery failure|returned mail|failure notice|address not found|mail delivery)",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-']+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
FINAL_RECIPIENT_RE = re.compile(r"Final-Recipient:\s*rfc822;\s*([^\s<>;]+@[^\s<>;]+)", re.IGNORECASE)
DIAGNOSTIC_RE = re.compile(r"Diagnostic-Code:\s*([^\n\r]+(?:[\n\r]+[ \t]+[^\n\r]+)*)", re.IGNORECASE)
ENHANCED_STATUS_RE = re.compile(r"\b([45])\.\d\.\d\b")
SMTP_STATUS_RE = re.compile(r"\b([45])\d{2}\b")
HARD_BOUNCE_TEXT_RE = re.compile(
    r"(permanent failure|recipient unknown|unknown recipient|user unknown|unknown user|"
    r"no such (?:user|recipient|mailbox)|mailbox unavailable|does not exist|"
    r"wasn't found|address rejected:\s*access denied|invalid (?:address|recipient|mailbox))",
    re.IGNORECASE,
)


@dataclass
class BounceScanResult:
    scanned_messages: int = 0
    detected_bounces: int = 0
    suppressed_emails: int = 0
    retry_eligible_bounces: int = 0
    errors: int = 0


def _logger() -> logging.Logger:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("bounce_scanner")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    handler = logging.FileHandler(settings.data_dir / "bounce_scan.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def _decode_header_value(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value)))
    except Exception:
        return value


def _message_text(message: Message) -> str:
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "message/delivery-status"}:
                continue
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            charset = part.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))
    else:
        payload = message.get_payload(decode=True)
        if payload is not None:
            charset = message.get_content_charset() or "utf-8"
            parts.append(payload.decode(charset, errors="replace"))
    return "\n".join(parts)


def _is_bounce(message_from: str, subject: str, body: str) -> bool:
    haystack = f"{message_from}\n{subject}\n{body[:2000]}"
    return bool(BOUNCE_FROM_RE.search(haystack) or BOUNCE_SUBJECT_RE.search(haystack))


def _extract_failed_recipients(body: str) -> set[str]:
    recipients = {match.group(1).strip(" <>.,;:").lower() for match in FINAL_RECIPIENT_RE.finditer(body)}
    if recipients:
        return recipients
    return {email.strip(" <>.,;:").lower() for email in EMAIL_RE.findall(body)}


def _extract_reason(body: str) -> str:
    match = DIAGNOSTIC_RE.search(body)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()[:500]
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    keywords = ("not found", "undeliver", "rejected", "blocked", "mailbox", "quota", "does not exist", "invalid")
    for line in lines:
        if any(keyword in line.lower() for keyword in keywords):
            return line[:500]
    return (lines[0] if lines else "Delivery failure detected.")[:500]


def _is_hard_bounce(reason: str, body: str) -> bool:
    haystack = f"{reason}\n{body[:5000]}"
    enhanced_statuses = ENHANCED_STATUS_RE.findall(haystack)
    if "5" in enhanced_statuses:
        return True
    if "4" in enhanced_statuses:
        return False
    smtp_statuses = SMTP_STATUS_RE.findall(haystack)
    if "5" in smtp_statuses:
        return True
    if "4" in smtp_statuses:
        return False
    return bool(HARD_BOUNCE_TEXT_RE.search(haystack))


def _imap_host_for_sender(sender: SenderAccount) -> tuple[str, int]:
    settings = get_settings()
    host = sender.smtp_host.lower().strip()
    if host == "smtp.feishu.cn":
        return "imap.feishu.cn", 993
    if host.startswith("smtp."):
        return f"imap.{host.removeprefix('smtp.')}", 993
    return settings.default_imap_host, settings.default_imap_port


def _sender_password(sender: SenderAccount) -> str:
    password = decrypt_secret(sender.smtp_password_encrypted)
    if password:
        return password
    return _read_env_value(sender.password_env or "")


def _message_date(message: Message) -> datetime:
    try:
        value = parsedate_to_datetime(message.get("Date", ""))
    except Exception:
        return utc_now()
    if value.tzinfo is None:
        return value.replace(tzinfo=utc_now().tzinfo)
    return value.astimezone(utc_now().tzinfo)


def _is_inside_lookback(message: Message, lookback_days: int | None) -> bool:
    if lookback_days is None or lookback_days <= 0:
        return True
    return _message_date(message) >= utc_now() - timedelta(days=lookback_days)


def _latest_send_for_recipient(session: Session, recipient: str, sender: SenderAccount) -> SendRecord | None:
    return session.exec(
        select(SendRecord)
        .where(
            func.lower(SendRecord.recipient_email) == recipient.lower(),
            SendRecord.sender_account_id == sender.id,
        )
        .order_by(SendRecord.sent_at.desc(), SendRecord.id.desc())
    ).first()


def _previous_send_was_bounced(session: Session, recipient: str, current: SendRecord) -> bool:
    if current.id is None:
        return False
    previous = session.exec(
        select(SendRecord)
        .where(
            func.lower(SendRecord.recipient_email) == recipient.lower(),
            SendRecord.id != current.id,
            or_(
                SendRecord.sent_at < current.sent_at,
                and_(SendRecord.sent_at == current.sent_at, SendRecord.id < current.id),
            ),
        )
        .order_by(SendRecord.sent_at.desc(), SendRecord.id.desc())
    ).first()
    return previous is not None and previous.smtp_status == "bounced"


def _removed_email_for_contact(contact_id: int) -> str:
    return f"removed-bounce-contact-{contact_id}@{REMOVED_EMAIL_DOMAIN}"


def _remove_bounced_contact_info(session: Session, contact: Contact, bounced_email: str) -> dict[str, int]:
    stats = {"contacts_preserved": 0, "emails_removed": 0, "drafts_deleted": 0, "send_records_deleted": 0, "routes_deleted": 0}
    drafts = session.exec(select(EmailDraft).where(EmailDraft.contact_id == contact.id)).all()
    draft_ids = [draft.id for draft in drafts if draft.id is not None]
    if draft_ids:
        session.exec(delete(EmailEvent).where(EmailEvent.draft_id.in_(draft_ids)))
        session.exec(delete(EmailDraft).where(EmailDraft.id.in_(draft_ids)))
        stats["drafts_deleted"] += len(draft_ids)
    stats["send_records_deleted"] += session.exec(delete(SendRecord).where(SendRecord.contact_id == contact.id)).rowcount or 0
    stats["routes_deleted"] += session.exec(
        delete(ContactRoute).where(
            ContactRoute.contact_id == contact.id,
            ContactRoute.route_type == "email",
            func.lower(ContactRoute.route_value) == bounced_email.lower(),
        )
    ).rowcount or 0
    session.exec(delete(DraftPrepItem).where(DraftPrepItem.contact_id == contact.id))
    if contact.email.lower() == bounced_email.lower():
        contact.email = _removed_email_for_contact(contact.id)
        contact.status = "bounced_email_removed"
        contact.updated_at = utc_now()
        session.add(contact)
        stats["emails_removed"] += 1
    stats["contacts_preserved"] += 1
    return stats


def cleanup_bounced_contacts(session: Session) -> dict[str, int]:
    stats = {
        "emails_processed": 0,
        "contacts_preserved": 0,
        "emails_removed": 0,
        "drafts_deleted": 0,
        "send_records_deleted": 0,
        "routes_deleted": 0,
    }
    bounced_emails = [
        email
        for email in session.exec(select(BounceRecord.recipient_email).distinct().order_by(BounceRecord.recipient_email)).all()
        if email
    ]
    for email in bounced_emails:
        normalized = email.strip().lower()
        suppression = session.exec(
            select(Suppression).where(func.lower(Suppression.email) == normalized)
        ).first()
        if suppression is None:
            # First non-hard bounce remains retry-eligible. BounceRecord is audit
            # evidence only until a hard bounce or consecutive second bounce.
            continue
        contact = session.exec(select(Contact).where(func.lower(Contact.email) == normalized)).first()
        if contact is None:
            continue
        stats["emails_processed"] += 1

        company = session.get(Company, contact.company_id)
        for record in session.exec(select(BounceRecord).where(func.lower(BounceRecord.recipient_email) == normalized)).all():
            record.send_record_id = None
            record.contact_id = None
            session.add(record)

        delete_stats = _remove_bounced_contact_info(session, contact, normalized)
        if company is not None and company.status == "bounced":
            company.status = "new"
            session.add(company)
        for key, value in delete_stats.items():
            stats[key] += value

    for company in session.exec(select(Company).where(Company.status == "bounced")).all():
        company.status = "new"
        session.add(company)
    session.commit()
    return stats


def _record_bounce(
    session: Session,
    sender: SenderAccount,
    uid: str,
    message: Message,
    recipient: str,
    send_record: SendRecord,
    body: str,
) -> str | None:
    existing = session.exec(
        select(BounceRecord).where(
            BounceRecord.sender_account_id == sender.id,
            BounceRecord.mailbox_uid == uid,
            BounceRecord.recipient_email == recipient,
        )
    ).first()
    if existing:
        return None

    now = utc_now()
    subject = _decode_header_value(message.get("Subject"))
    message_from = _decode_header_value(message.get("From"))
    reason = _extract_reason(body)
    hard_bounce = _is_hard_bounce(reason, body)
    consecutive_second_bounce = not hard_bounce and _previous_send_was_bounced(
        session, recipient, send_record
    )
    should_suppress = hard_bounce or consecutive_second_bounce
    decision = (
        "hard_suppressed"
        if hard_bounce
        else "repeat_suppressed"
        if consecutive_second_bounce
        else "soft_retry"
    )
    excerpt = re.sub(r"\s+", " ", body).strip()[:1200]
    session.add(
        BounceRecord(
            send_record_id=send_record.id,
            company_id=send_record.company_id,
            contact_id=send_record.contact_id,
            sender_account_id=sender.id,
            sender_email=sender.email,
            recipient_email=recipient,
            mailbox_uid=uid,
            bounce_subject=subject,
            bounce_from=message_from,
            bounce_reason=reason,
            raw_excerpt=excerpt,
            detected_at=now,
        )
    )

    send_record.smtp_status = "bounced"
    session.add(send_record)

    if should_suppress:
        contact = session.get(Contact, send_record.contact_id)
        if contact:
            contact.status = "bounced"
            contact.updated_at = now
            session.add(contact)

        company = session.get(Company, send_record.company_id)
        if company and company.status not in {"replied", "unsubscribed", "blacklisted"}:
            company.status = "bounced"
            company.next_follow_up_at = None
            company.updated_at = now
            session.add(company)

        existing_suppression = session.exec(
            select(Suppression).where(func.lower(Suppression.email) == recipient.lower())
        ).first()
        if existing_suppression is None:
            prefix = "hard bounce" if hard_bounce else "consecutive second bounce"
            session.add(Suppression(email=recipient, reason=f"{prefix}: {reason[:160]}"))

    if send_record.draft_id:
        session.add(
            EmailEvent(
                draft_id=send_record.draft_id,
                event_type="bounce_detected",
                metadata_json=json.dumps(
                    {
                        "recipient_email": recipient,
                        "sender_email": sender.email,
                        "mailbox_uid": uid,
                        "reason": reason,
                        "subject": subject,
                        "decision": decision,
                    },
                    ensure_ascii=False,
                ),
            )
        )
    return decision


def scan_sender_bounces(session: Session, sender: SenderAccount, lookback_days: int | None = None) -> BounceScanResult:
    result = BounceScanResult()
    logger = _logger()
    password = _sender_password(sender)
    if not password:
        logger.warning("skip sender=%s reason=missing_password", sender.email)
        result.errors += 1
        return result

    host, port = _imap_host_for_sender(sender)
    lookback = lookback_days if lookback_days is not None else get_settings().bounce_scan_lookback_days
    try:
        with imaplib.IMAP4_SSL(host, port, timeout=30) as mailbox:
            mailbox.login(sender.smtp_username or sender.email, password)
            mailbox.select("INBOX", readonly=True)
            status, search_data = mailbox.uid("SEARCH", None, "ALL")
            if status != "OK":
                raise RuntimeError(f"IMAP search failed: {status}")
            uids = search_data[0].split() if search_data and search_data[0] else []
            for uid_bytes in uids:
                uid = uid_bytes.decode("ascii", errors="replace")
                status, fetch_data = mailbox.uid("FETCH", uid, "(BODY.PEEK[])")
                if status != "OK" or not fetch_data:
                    result.errors += 1
                    continue
                raw_message = next((item[1] for item in fetch_data if isinstance(item, tuple)), None)
                if not raw_message:
                    continue
                result.scanned_messages += 1
                message = message_from_bytes(raw_message)
                subject = _decode_header_value(message.get("Subject"))
                message_from = _decode_header_value(message.get("From"))
                body = _message_text(message)
                if not _is_inside_lookback(message, lookback):
                    continue
                if not _is_bounce(message_from, subject, body):
                    continue
                result.detected_bounces += 1
                recipients = _extract_failed_recipients(body)
                for recipient in sorted(recipients):
                    send_record = _latest_send_for_recipient(session, recipient, sender)
                    if send_record is None:
                        continue
                    decision = _record_bounce(session, sender, uid, message, recipient, send_record, body)
                    if decision:
                        if decision == "soft_retry":
                            result.retry_eligible_bounces += 1
                        else:
                            result.suppressed_emails += 1
                        logger.info(
                            "bounce decision=%s sender=%s recipient=%s send_record_id=%s uid=%s reason=%s",
                            decision,
                            sender.email,
                            recipient,
                            send_record.id,
                            uid,
                            _extract_reason(body),
                        )
            session.commit()
    except Exception as exc:
        session.rollback()
        result.errors += 1
        logger.exception("bounce scan failed sender=%s host=%s error=%s", sender.email, host, exc)
    return result


def scan_bounces(session: Session, sender_id: int | None = None) -> dict[str, int]:
    statement = select(SenderAccount).where(SenderAccount.is_active == True)
    if sender_id is not None:
        statement = statement.where(SenderAccount.id == sender_id)
    totals = BounceScanResult()
    for sender in session.exec(statement).all():
        result = scan_sender_bounces(session, sender)
        totals.scanned_messages += result.scanned_messages
        totals.detected_bounces += result.detected_bounces
        totals.suppressed_emails += result.suppressed_emails
        totals.retry_eligible_bounces += result.retry_eligible_bounces
        totals.errors += result.errors
    cleanup = cleanup_bounced_contacts(session)
    return {
        "scanned_messages": totals.scanned_messages,
        "detected_bounces": totals.detected_bounces,
        "suppressed_emails": totals.suppressed_emails,
        "retry_eligible_bounces": totals.retry_eligible_bounces,
        "errors": totals.errors,
        **cleanup,
    }


def bounce_log_path() -> Path:
    return get_settings().data_dir / "bounce_scan.log"
