import random
import re
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Company, Contact, EmailDraft, EmailEvent, EmailTemplate, SendRecord, SenderAccount, Suppression
from app.services.email_quality import validate_contact_email
from app.services.feishu_native_reply import is_native_reply_draft, native_reply_metadata, send_queued_native_reply
from app.services.app_settings import is_queue_paused
from app.services.mailer import send_draft
from app.services.mailer import validate_attachment_paths
from app.services.no_go_companies import is_no_go_company
from app.services.queue_state import reconcile_queue_state
from app.services.render_validation import SWITCH_TEMPLATE_GUIDANCE, rendered_message_field_issues
from app.services.signatures import get_signature_template
from app.services.suppression import is_active_suppression
from app.services.templates import render_template
from app.time_utils import local_day_bounds_utc, utc_now
from app.services.followup_cadence import scan_staged_followups

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_SEND_INTERVAL_SECONDS = 30
NATIVE_REPLY_GLOBAL_MIN_INTERVAL_SECONDS = 45
SHARED_RECIPIENT_DOMAINS = {
    "aol.com", "btconnect.com", "free.fr", "gmail.com", "googlemail.com", "hotmail.com",
    "hotmail.co.uk", "icloud.com", "laposte.net", "live.com", "orange.fr", "outlook.com",
    "proton.me", "protonmail.com", "wanadoo.fr", "yahoo.com", "yahoo.co.uk",
}


def sender_timezone(sender: SenderAccount) -> ZoneInfo:
    """Return the sender-local timezone, retaining the app timezone for legacy rows."""
    timezone_name = (sender.send_timezone or get_settings().app_timezone).strip()
    try:
        return ZoneInfo(timezone_name)
    except Exception:
        return ZoneInfo(get_settings().app_timezone)


def validate_sender_timezone(value: str) -> str:
    timezone_name = (value or "").strip()
    if not timezone_name:
        raise ValueError("Sender timezone is required.")
    try:
        ZoneInfo(timezone_name)
    except Exception as exc:
        raise ValueError("Sender timezone must be a valid IANA name, such as Europe/London.") from exc
    return timezone_name


def _parse_window_time(value: str):
    value = value.strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise ValueError("Each send window must use HH:MM-HH:MM format.")


def parse_sender_windows_text(value: str, *, fallback_start=None, fallback_end=None) -> str:
    raw = (value or "").replace("\n", ",").strip(" ,")
    if not raw:
        if fallback_start is None or fallback_end is None:
            raise ValueError("At least one send window is required.")
        raw = f"{fallback_start.strftime('%H:%M')}-{fallback_end.strftime('%H:%M')}"
    windows: list[tuple[object, object]] = []
    for entry in [part.strip() for part in raw.split(",") if part.strip()]:
        match = re.fullmatch(r"(\d{1,2}:\d{2}(?::\d{2})?)\s*-\s*(\d{1,2}:\d{2}(?::\d{2})?)", entry)
        if not match:
            raise ValueError("Use comma-separated daytime windows, for example 09:30-11:30, 14:00-16:30.")
        start, end = _parse_window_time(match.group(1)), _parse_window_time(match.group(2))
        if start >= end:
            raise ValueError("Multiple send windows must be daytime ranges with start before end.")
        windows.append((start, end))
    if not windows or len(windows) > 8:
        raise ValueError("Configure between 1 and 8 send windows.")
    windows.sort(key=lambda item: item[0])
    for previous, current in zip(windows, windows[1:]):
        if current[0] <= previous[1]:
            raise ValueError("Send windows cannot overlap or touch.")
    return json.dumps([{"start": start.strftime("%H:%M"), "end": end.strftime("%H:%M")} for start, end in windows], separators=(",", ":"))


def sender_windows(sender: SenderAccount) -> list[tuple[object, object]]:
    try:
        payload = json.loads(sender.send_windows_json or "")
        if isinstance(payload, list) and payload:
            parsed = [(_parse_window_time(item["start"]), _parse_window_time(item["end"])) for item in payload]
            if all(start < end for start, end in parsed):
                return sorted(parsed, key=lambda item: item[0])
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        pass
    return [(sender.window_start, sender.window_end)]


def format_sender_windows(sender: SenderAccount) -> str:
    return ", ".join(f"{start.strftime('%H:%M')}-{end.strftime('%H:%M')}" for start, end in sender_windows(sender))


def recipient_company_domain(email: str | None) -> str | None:
    normalized = (email or "").strip().lower()
    if normalized.count("@") != 1:
        return None
    domain = normalized.rsplit("@", 1)[1].strip(".")
    if not domain or domain in SHARED_RECIPIENT_DOMAINS:
        return None
    return domain


def audit_draft(session: Session, draft: EmailDraft) -> list[str]:
    contact = session.get(Contact, draft.contact_id)
    company = session.get(Company, draft.company_id)
    issues: list[str] = []
    rendered_field_issues = rendered_message_field_issues(
        subject=draft.subject,
        body_html=draft.body_html,
        body_text=draft.body_text,
        recipient_email=contact.email if contact else None,
        cc_emails=draft.cc_emails,
    )
    if rendered_field_issues:
        issues.append(
            f"Template field resolution failed: {'; '.join(rendered_field_issues)}. "
            f"{SWITCH_TEMPLATE_GUIDANCE}"
        )
    if contact is None:
        issues.append("Contact not found.")
    else:
        email_check = validate_contact_email(contact.email.strip(), check_deliverability=True)
        if email_check.is_blocking:
            issues.append(f"Recipient email quality check failed: {email_check.reason}")
        elif not EMAIL_RE.match(contact.email.strip()):
            issues.append(f"Invalid recipient email: {contact.email}")
        elif (suppression := session.exec(select(Suppression).where(Suppression.email == contact.email.strip().lower())).first()) and is_active_suppression(suppression):
            issues.append(f"Recipient is in suppression list: {contact.email}")
    if company is None:
        issues.append("Company not found.")
    elif is_no_go_company(company):
        issues.append(f"Company is in NO-GO list: {company.name}")
    if draft.cc_emails:
        invalid_cc = [
            email.strip()
            for email in draft.cc_emails.replace(";", ",").split(",")
            if email.strip() and not EMAIL_RE.match(email.strip())
        ]
        if invalid_cc:
            issues.append(f"Invalid CC email: {', '.join(invalid_cc)}")
    issues.extend(validate_attachment_paths(draft.attachment_paths))
    if not draft.subject.strip():
        issues.append("Subject is empty.")
    if not draft.body_html.strip():
        issues.append("Email body is empty.")
    return issues


def apply_audit_result(session: Session, draft: EmailDraft) -> EmailDraft:
    issues = audit_draft(session, draft)
    now = utc_now()
    if issues:
        draft.status = "pending_review"
        draft.error_message = "; ".join(issues)
        draft.approved_at = None
        session.add(EmailEvent(draft_id=draft.id, event_type="audit_exception", metadata_json=draft.error_message))
    else:
        draft.status = "approved"
        draft.error_message = None
        draft.approved_at = now
        session.add(EmailEvent(draft_id=draft.id, event_type="auto_approved"))
    draft.updated_at = now
    session.add(draft)
    return draft


def refresh_pending_audits(session: Session) -> None:
    pending = session.exec(select(EmailDraft).where(EmailDraft.status == "pending_review")).all()
    changed = False
    for draft in pending:
        if not draft.error_message:
            apply_audit_result(session, draft)
            changed = True
    if changed:
        session.commit()


def approve_draft(session: Session, draft_id: int) -> EmailDraft:
    draft = session.get(EmailDraft, draft_id)
    if draft is None:
        raise ValueError("Draft not found.")
    if draft.status not in {"pending_review", "approved"}:
        raise ValueError("Only draft audit exceptions can be marked approved.")
    draft.status = "approved"
    draft.approved_at = utc_now()
    draft.updated_at = utc_now()
    draft.error_message = None
    session.add(draft)
    session.add(EmailEvent(draft_id=draft.id, event_type="manual_exception_approved"))
    session.commit()
    session.refresh(draft)
    return draft


def daily_sent_count(session: Session, sender: SenderAccount | int) -> int:
    sender_id = sender.id if isinstance(sender, SenderAccount) else sender
    if isinstance(sender, SenderAccount):
        local_now = utc_now().astimezone(sender_timezone(sender))
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start = start_local.astimezone(timezone.utc)
        end = (start_local + timedelta(days=1)).astimezone(timezone.utc)
    else:
        start, end = local_day_bounds_utc()
    return session.exec(
        select(func.count(SendRecord.id)).where(
            SendRecord.sender_account_id == sender_id,
            SendRecord.sent_at >= start,
            SendRecord.sent_at < end,
        )
    ).one()


def next_window_start(sender: SenderAccount, now: datetime | None = None) -> datetime:
    now = now or utc_now()
    local_timezone = sender_timezone(sender)
    local_now = now.astimezone(local_timezone)
    windows = sender_windows(sender)
    for window_start, window_end in windows:
        if _is_time_inside_window(local_now.time(), window_start, window_end):
            return now
        if local_now.time() < window_start:
            return datetime.combine(local_now.date(), window_start, tzinfo=local_timezone).astimezone(timezone.utc)
    return datetime.combine(local_now.date() + timedelta(days=1), windows[0][0], tzinfo=local_timezone).astimezone(timezone.utc)


def next_daily_window_start(sender: SenderAccount, now: datetime | None = None) -> datetime:
    now = now or utc_now()
    local_timezone = sender_timezone(sender)
    local_now = now.astimezone(local_timezone)
    start_next_day = datetime.combine(local_now.date() + timedelta(days=1), sender_windows(sender)[0][0], tzinfo=local_timezone)
    return start_next_day.astimezone(timezone.utc)


def next_allowed_send_time(sender: SenderAccount, candidate: datetime) -> datetime:
    return next_window_start(sender, candidate)


def sender_interval_seconds(sender: SenderAccount) -> int:
    configured = random.randint(sender.random_delay_min_seconds, sender.random_delay_max_seconds)
    return max(configured, MIN_SEND_INTERVAL_SECONDS)


def _as_aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _ordered_sender_queue(session: Session, sender_id: int) -> list[EmailDraft]:
    return session.exec(
        select(EmailDraft)
        .where(EmailDraft.status == "queued", EmailDraft.sender_account_id == sender_id)
        .order_by(EmailDraft.scheduled_at, EmailDraft.id)
    ).all()


def _next_sender_queue_slot(session: Session, sender: SenderAccount) -> datetime:
    candidate = next_window_start(sender)
    if daily_sent_count(session, sender) >= sender.daily_limit:
        candidate = next_daily_window_start(sender)

    latest = session.exec(
        select(EmailDraft)
        .where(EmailDraft.status == "queued", EmailDraft.sender_account_id == sender.id)
        .order_by(EmailDraft.scheduled_at.desc(), EmailDraft.id.desc())
    ).first()
    if latest and latest.scheduled_at:
        latest_at = _as_aware_utc(latest.scheduled_at)
        next_after_latest = latest_at + timedelta(
            seconds=sender_interval_seconds(sender)
        )
        if next_after_latest > candidate:
            return next_allowed_send_time(sender, next_after_latest)
    return candidate


def _reschedule_sender_queue_from(session: Session, sender: SenderAccount, start_at: datetime, event_type: str) -> int:
    schedule_at = start_at
    queued_drafts = _ordered_sender_queue(session, sender.id)
    for draft in queued_drafts:
        draft.scheduled_at = schedule_at
        draft.updated_at = utc_now()
        session.add(draft)
        session.add(EmailEvent(draft_id=draft.id, event_type=event_type))
        schedule_at = next_allowed_send_time(sender, schedule_at + timedelta(seconds=sender_interval_seconds(sender)))
    return len(queued_drafts)


def _append_draft_to_sender_queue_tail(
    session: Session,
    sender: SenderAccount,
    draft: EmailDraft,
    earliest_at: datetime,
) -> datetime:
    """Move one delayed draft behind this sender's current queued tail.

    This repairs an overdue draft without disturbing the schedule of other
    queued work.  Each subsequent delayed draft observes the new tail and is
    appended after it with its own configured randomized interval.
    """
    tail = session.exec(
        select(EmailDraft)
        .where(
            EmailDraft.status == "queued",
            EmailDraft.sender_account_id == sender.id,
            EmailDraft.id != draft.id,
            EmailDraft.scheduled_at.is_not(None),
        )
        .order_by(EmailDraft.scheduled_at.desc(), EmailDraft.id.desc())
    ).first()
    scheduled_at = earliest_at
    if tail and tail.scheduled_at:
        after_tail = _as_aware_utc(tail.scheduled_at) + timedelta(
            seconds=sender_interval_seconds(sender)
        )
        if after_tail > scheduled_at:
            scheduled_at = next_allowed_send_time(sender, after_tail)
    draft.scheduled_at = scheduled_at
    draft.updated_at = utc_now()
    session.add(draft)
    session.add(EmailEvent(draft_id=draft.id, event_type="sender_interval_tail_deferred"))
    return scheduled_at


def _last_sent_at(session: Session, sender_id: int) -> datetime | None:
    value = session.exec(
        select(SendRecord.sent_at)
        .where(
            SendRecord.sender_account_id == sender_id,
        )
        .order_by(SendRecord.sent_at.desc())
    ).first()
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _last_native_reply_sent_at(session: Session) -> datetime | None:
    value = session.exec(
        select(EmailDraft.sent_at)
        .where(
            EmailDraft.status == "sent",
            EmailDraft.sent_at.is_not(None),
            EmailDraft.template_snapshot.contains("feishu_native_reply"),
        )
        .order_by(EmailDraft.sent_at.desc())
    ).first()
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def recent_company_send_records(session: Session, company_ids: set[int], hours: int = 8) -> list[SendRecord]:
    if not company_ids:
        return []
    cutoff = utc_now() - timedelta(hours=hours)
    records = session.exec(
        select(SendRecord)
        .where(
            SendRecord.company_id.in_(company_ids),
            SendRecord.sent_at >= cutoff,
        )
        .order_by(SendRecord.sent_at.desc())
    ).all()
    return records


def queue_draft(
    session: Session,
    draft_id: int,
    sender_id: int,
    scheduled_at: datetime | None = None,
    commit: bool = True,
) -> EmailDraft:
    draft = session.get(EmailDraft, draft_id)
    sender = session.get(SenderAccount, sender_id)
    if draft is None or sender is None:
        raise ValueError("Draft or sender not found.")
    if draft.status != "approved":
        raise ValueError("Draft must be marked handled before queuing.")
    existing_company_queue = session.exec(
        select(EmailDraft).where(
            EmailDraft.company_id == draft.company_id,
            EmailDraft.status == "queued",
            EmailDraft.id != draft.id,
        )
    ).first()
    if existing_company_queue:
        raise ValueError("Company already has a queued draft. One company can only have one draft in a send batch.")
    contact = session.get(Contact, draft.contact_id)
    target_domain = recipient_company_domain(contact.email if contact else None)
    if target_domain:
        queued_emails = session.exec(
            select(Contact.email)
            .join(EmailDraft, EmailDraft.contact_id == Contact.id)
            .where(EmailDraft.status == "queued", EmailDraft.id != draft.id)
        ).all()
        if any(recipient_company_domain(email) == target_domain for email in queued_emails):
            raise ValueError(
                f"Enterprise email domain already has a queued draft: {target_domain}. "
                "One enterprise can only have one draft in a send batch."
            )
    refresh_draft_from_current_template(session, draft, sender=sender)
    issues = audit_draft(session, draft)
    if issues:
        draft.status = "pending_review"
        draft.sender_account_id = None
        draft.scheduled_at = None
        draft.approved_at = None
        draft.error_message = "; ".join(issues)
        draft.updated_at = utc_now()
        session.add(draft)
        session.add(EmailEvent(draft_id=draft.id, event_type="queue_blocked", metadata_json=draft.error_message))
        if commit:
            session.commit()
        raise ValueError(draft.error_message)
    draft.sender_account_id = sender.id
    draft.status = "queued"
    draft.scheduled_at = scheduled_at or _next_sender_queue_slot(session, sender)
    draft.updated_at = utc_now()
    session.add(draft)
    session.add(EmailEvent(draft_id=draft.id, event_type="queued"))
    if commit:
        session.commit()
        session.refresh(draft)
    return draft


def cancel_queued_draft(session: Session, draft: EmailDraft) -> None:
    if draft.status != "queued":
        raise ValueError("Only queued drafts can be cancelled.")
    draft.status = "approved"
    draft.sender_account_id = None
    draft.scheduled_at = None
    draft.updated_at = utc_now()
    session.add(draft)
    session.add(EmailEvent(draft_id=draft.id, event_type="queue_cancelled"))


def refresh_draft_from_template(
    session: Session,
    draft: EmailDraft,
    template: EmailTemplate,
    sender: SenderAccount | None = None,
) -> None:
    company = session.get(Company, draft.company_id)
    contact = session.get(Contact, draft.contact_id)
    if company is None or contact is None:
        raise ValueError("Missing company or contact.")
    subject, html, text, snapshot = render_template(template, company, contact, session=session, sender=sender)
    signature_template = get_signature_template(session)
    draft.template_id = template.id
    draft.signature_template_id = signature_template.id if signature_template else None
    draft.subject = subject
    draft.body_html = html
    draft.body_text = text
    if template.cc_enabled and template.cc_emails:
        draft.cc_emails = template.cc_emails
    draft.template_snapshot = snapshot
    draft.updated_at = utc_now()
    session.add(draft)
    session.add(EmailEvent(draft_id=draft.id, event_type="template_refreshed_before_send"))


def refresh_draft_from_current_template(session: Session, draft: EmailDraft, sender: SenderAccount | None = None) -> None:
    if draft.template_id is None:
        return
    template = session.get(EmailTemplate, draft.template_id)
    if template is None:
        return
    refresh_draft_from_template(session, draft, template, sender=sender)


def _is_time_inside_window(current_time, window_start, window_end) -> bool:
    if window_start == window_end:
        return True
    if window_start < window_end:
        return window_start <= current_time <= window_end
    return current_time >= window_start or current_time <= window_end


def _inside_sender_window(sender: SenderAccount, now: datetime) -> bool:
    local = now.astimezone(sender_timezone(sender)).time()
    return any(_is_time_inside_window(local, window_start, window_end) for window_start, window_end in sender_windows(sender))


def process_due_queue(session: Session, limit: int = 10, draft_ids: list[int] | None = None) -> dict[str, int | bool]:
    if is_queue_paused(session):
        return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0, "deferred": 0, "paused": True}

    statement = (
        select(EmailDraft)
        .where(EmailDraft.status == "queued", EmailDraft.scheduled_at <= utc_now())
        .order_by(EmailDraft.scheduled_at, EmailDraft.id)
        .limit(limit)
    )
    if draft_ids:
        statement = statement.where(EmailDraft.id.in_(draft_ids))
    due = session.exec(statement).all()
    sent = 0
    failed = 0
    skipped = 0
    deferred = 0
    deferred_sender_ids: set[int] = set()

    for draft in due:
        sender = session.get(SenderAccount, draft.sender_account_id) if draft.sender_account_id else None
        contact = session.get(Contact, draft.contact_id)
        if sender is None or contact is None:
            draft.status = "failed"
            draft.error_message = "Missing sender or contact."
            failed += 1
        else:
            try:
                refresh_draft_from_current_template(session, draft, sender=sender)
                audit_issues = audit_draft(session, draft)
            except Exception as exc:
                audit_issues = [f"Template refresh failed: {exc}"]
            if audit_issues:
                draft.status = "pending_review"
                draft.sender_account_id = None
                draft.scheduled_at = None
                draft.error_message = "; ".join(audit_issues)
                session.add(EmailEvent(draft_id=draft.id, event_type="template_refresh_blocked", metadata_json=draft.error_message))
                failed += 1
            elif not _inside_sender_window(sender, utc_now()):
                draft.scheduled_at = next_window_start(sender)
            elif daily_sent_count(session, sender) >= sender.daily_limit:
                if sender.id not in deferred_sender_ids:
                    deferred += _reschedule_sender_queue_from(
                        session,
                        sender,
                        next_daily_window_start(sender),
                        "daily_limit_deferred",
                    )
                    deferred_sender_ids.add(sender.id)
            elif (suppression := session.exec(select(Suppression).where(Suppression.email == contact.email.lower())).first()) and is_active_suppression(suppression):
                draft.status = "skipped"
                draft.error_message = "Suppressed email."
                skipped += 1
            else:
                is_native_reply = is_native_reply_draft(draft.template_snapshot)
                if is_native_reply:
                    last_native_sent_at = _last_native_reply_sent_at(session)
                    if last_native_sent_at is not None:
                        next_native_send_at = last_native_sent_at + timedelta(
                            seconds=NATIVE_REPLY_GLOBAL_MIN_INTERVAL_SECONDS
                        )
                        if utc_now() < next_native_send_at:
                            draft.scheduled_at = next_native_send_at
                            draft.updated_at = utc_now()
                            session.add(draft)
                            continue
                last_sent_at = _last_sent_at(session, sender.id)
                if last_sent_at is not None:
                    # A backlog can make several drafts due at the same time.  Enforce
                    # the sender's configured randomized interval at send time as well
                    # as when the queue is initially scheduled, otherwise the fixed
                    # 30-second safety floor lets an overdue queue burst.
                    next_allowed_at = last_sent_at + timedelta(seconds=sender_interval_seconds(sender))
                    if utc_now() < next_allowed_at:
                        _append_draft_to_sender_queue_tail(
                            session,
                            sender,
                            draft,
                            next_allowed_at,
                        )
                        continue
                try:
                    native_reply_result = None
                    if is_native_reply:
                        native_reply_result = send_queued_native_reply(draft, sender, contact)
                        send_result = "sent"
                    else:
                        send_result = send_draft(session, draft, sender)
                    now = utc_now()
                    draft.status = "sent" if send_result == "sent" else "simulated"
                    draft.sent_at = now
                    draft.updated_at = now
                    company = session.get(Company, draft.company_id)
                    if company:
                        if send_result == "sent":
                            company.last_contact_at = now
                        company.updated_at = now
                        session.add(company)
                    session.add(
                        EmailEvent(
                            draft_id=draft.id,
                            event_type="feishu_native_reply_sent" if is_native_reply else draft.status,
                            metadata_json=json.dumps(native_reply_result, ensure_ascii=False) if native_reply_result else None,
                        )
                    )
                    if send_result == "sent":
                        session.add(
                            SendRecord(
                                draft_id=draft.id,
                                company_id=draft.company_id,
                                contact_id=draft.contact_id,
                                sender_account_id=sender.id,
                                template_id=draft.template_id,
                                signature_template_id=draft.signature_template_id,
                                sender_email=sender.email,
                                recipient_email=contact.email,
                                subject=draft.subject,
                                body_html=draft.body_html if is_native_reply else "",
                                body_text=draft.body_text if is_native_reply else None,
                                cc_emails=draft.cc_emails,
                                attachment_paths=draft.attachment_paths,
                                smtp_status="sent",
                                sent_at=now,
                            )
                        )
                        sent += 1
                    if send_result in {"sent", "simulated"}:
                        draft.body_html = ""
                        draft.body_text = None
                        if is_native_reply:
                            snapshot = native_reply_metadata(draft.template_snapshot)
                            snapshot["delivery_result"] = native_reply_result
                            draft.template_snapshot = json.dumps(snapshot, ensure_ascii=False)
                        else:
                            draft.template_snapshot = json.dumps(
                                {
                                    "template_id": draft.template_id,
                                    "signature_template_id": draft.signature_template_id,
                                },
                                ensure_ascii=False,
                            )
                except Exception as exc:
                    draft.status = "failed"
                    draft.error_message = str(exc)
                    failed += 1
        draft.updated_at = utc_now()
        session.add(draft)
    session.commit()
    reconcile_queue_state(session)
    return {"processed": len(due), "sent": sent, "failed": failed, "skipped": skipped, "deferred": deferred, "paused": False}


def scan_followups(session: Session) -> dict[str, int]:
    staged_result = scan_staged_followups(session)
    # Fixed-delay FollowUpRule rows are retired: the dual-counter cadence is the
    # only automated route.  A disabled cadence therefore creates nothing.
    return staged_result or {"followup_drafts": 0}
