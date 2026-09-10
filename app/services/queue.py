import random
import re
import json
from datetime import datetime, timedelta, timezone
from threading import Lock
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Company, Contact, EmailDraft, EmailEvent, EmailTemplate, QueueHandoffRun, SendRecord, SenderAccount, Suppression
from app.services.email_quality import validate_contact_email
from app.services.feishu_native_reply import is_native_reply_draft, native_reply_metadata, send_queued_native_reply
from app.services.app_settings import is_queue_paused, set_app_setting
from app.services.mailer import send_draft
from app.services.delivery_outcome import DeliveryUncertainError
from app.services.mailer import validate_attachment_paths
from app.services.no_go_companies import is_no_go_company
from app.services.render_validation import (
    SWITCH_TEMPLATE_GUIDANCE,
    is_specific_company_region,
    rendered_message_field_issues,
    template_references_field,
)
from app.services.signatures import get_signature_template
from app.services.suppression import suppression_matches_email
from app.services.template_targeting import template_target_issues
from app.services.templates import render_template
from app.time_utils import local_day_bounds_utc, utc_now
from app.services.followup_cadence import scan_staged_followups

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_SEND_INTERVAL_SECONDS = 30
RECENT_DUPLICATE_SEND_HOURS = 24
NATIVE_REPLY_GLOBAL_MIN_INTERVAL_SECONDS = 45
# Both sending and intake mutate the same SQLite queue.  One in-process gate
# makes their write ownership explicit; database busy_timeout covers a second
# process or a transient external reader/writer.
QUEUE_PROCESSING_LOCK = Lock()
QUEUE_HANDOFF_BATCH_SIZE = 50
SHARED_RECIPIENT_DOMAINS = {
    "aol.com", "btconnect.com", "free.fr", "gmail.com", "googlemail.com", "hotmail.com",
    "hotmail.co.uk", "icloud.com", "laposte.net", "live.com", "orange.fr", "outlook.com",
    "proton.me", "protonmail.com", "wanadoo.fr", "yahoo.com", "yahoo.co.uk",
    "qq.com", "163.com", "126.com",
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


def _cc_recipients(value: str | None) -> list[str]:
    return [email.strip() for email in (value or "").replace(";", ",").split(",") if email.strip()]


def _active_suppression_for_email(session: Session, email: str | None) -> Suppression | None:
    normalized = (email or "").strip().lower()
    if not normalized:
        return None
    domain = normalized.rsplit("@", 1)[-1]
    candidates = session.exec(
        select(Suppression).where(func.lower(Suppression.email).in_([normalized, f"@{domain}"]))
    ).all()
    return next((item for item in candidates if suppression_matches_email(item, normalized)), None)


def resolve_primary_contact(session: Session, company_id: str) -> Contact | None:
    """Choose one company contact with a stable, documented ordering."""
    contacts = session.exec(
        select(Contact).where(Contact.company_id == company_id, Contact.status == "active")
    ).all()
    if not contacts:
        return None

    def rank(contact: Contact) -> tuple[int, int, int, int]:
        email = (contact.email or "").strip().casefold()
        generic = email.startswith(("info@", "sales@", "contact@", "hello@", "enquiries@", "office@"))
        try:
            priority_rank = int(contact.priority_contact_rank)
        except (TypeError, ValueError):
            priority_rank = 999999
        return (
            0 if contact.is_primary else 1,
            priority_rank,
            1 if generic else 0,
            contact.id or 0,
        )

    return min(contacts, key=rank)


def sanitize_cc_once(session: Session, draft: EmailDraft) -> list[dict[str, str]]:
    """Remove unsafe CCs once, at queue intake, without blocking the To recipient."""
    kept: list[str] = []
    removed: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_email in _cc_recipients(draft.cc_emails):
        email = raw_email.casefold()
        if email in seen:
            removed.append({"email": raw_email, "reason": "duplicate"})
            continue
        seen.add(email)
        if not EMAIL_RE.match(raw_email):
            removed.append({"email": raw_email, "reason": "invalid_format"})
            continue
        email_check = validate_contact_email(raw_email, check_deliverability=True)
        if email_check.is_blocking:
            removed.append({"email": raw_email, "reason": f"quality:{email_check.reason}"})
            continue
        suppression = _active_suppression_for_email(session, raw_email)
        if suppression:
            removed.append({"email": raw_email, "reason": f"suppressed:{suppression.reason}"})
            continue
        kept.append(raw_email)
    draft.cc_emails = ", ".join(kept) or None
    if removed:
        session.add(
            EmailEvent(
                draft_id=draft.id,
                event_type="cc_recipient_removed",
                metadata_json=json.dumps({"removed": removed}, ensure_ascii=False),
            )
        )
    return removed


def _company_blocks_draft(company: Company, draft: EmailDraft) -> bool:
    # A reply ends cold outreach, but does not forbid an explicitly prepared reply.
    if is_native_reply_draft(draft.template_snapshot) and company.status == "replied" and not company.is_blacklisted:
        return False
    return is_no_go_company(company)


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
        if contact.status != "active":
            issues.append("Recipient contact is not active.")
        if contact.company_id != draft.company_id:
            issues.append("Recipient no longer belongs to the approved company.")
        contact_email = (contact.email or "").strip()
        if not contact_email:
            issues.append("Missing recipient email.")
        else:
            email_check = validate_contact_email(contact_email, check_deliverability=True)
            if email_check.is_blocking:
                issues.append(f"Recipient email quality check failed: {email_check.reason}")
            elif not EMAIL_RE.match(contact_email):
                issues.append(f"Invalid recipient email: {contact.email}")
            elif _active_suppression_for_email(session, contact_email):
                issues.append(f"Recipient is in suppression list: {contact.email}")
    if company is None:
        issues.append("Company not found.")
    elif _company_blocks_draft(company, draft):
        issues.append(f"Company is in NO-GO list: {company.name}")
    elif draft.template_id is not None:
        template = session.get(EmailTemplate, draft.template_id)
        if template:
            if template_references_field(template, "region") and not is_specific_company_region(company.region):
                issues.append(
                    "Template requires a specific company region, but the current region is "
                    f"{company.region!r}. {SWITCH_TEMPLATE_GUIDANCE}"
                )
            issues.extend(template_target_issues(template, company))
    if draft.sender_account_id is not None:
        sender = session.get(SenderAccount, draft.sender_account_id)
        if sender is None or not sender.is_active:
            issues.append("Sender account is not active.")
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


def _unresolved_attempts(sender_id=None):
    statement = select(EmailEvent.occurred_at).join(EmailDraft, EmailDraft.id == EmailEvent.draft_id).where(
        EmailEvent.event_type == "send_attempt_started", EmailDraft.status.in_(["sending", "send_unknown"]))
    if sender_id is not None:
        statement = statement.where(EmailDraft.sender_account_id == sender_id)
    return statement


def daily_capacity_count(session: Session, sender: SenderAccount) -> int:
    local = utc_now().astimezone(sender_timezone(sender))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    end = (local.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)).astimezone(timezone.utc)
    unresolved = session.exec(_unresolved_attempts(sender.id).where(
        EmailEvent.occurred_at >= start, EmailEvent.occurred_at < end)).all()
    return daily_sent_count(session, sender) + len(unresolved)


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
    if daily_capacity_count(session, sender) >= sender.daily_limit:
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


def _last_global_sent_at(session: Session) -> datetime | None:
    value = session.exec(
        select(SendRecord.sent_at).order_by(SendRecord.sent_at.desc())
    ).first()
    unresolved = session.exec(_unresolved_attempts().order_by(EmailEvent.occurred_at.desc())).first()
    return max((_as_aware_utc(item) for item in (value, unresolved) if item is not None), default=None)


def reserve_sender_day_slot(session: Session, sender: SenderAccount, candidate: datetime, *, exclude_draft_id=None) -> datetime:
    if sender.daily_limit < 1:
        raise ValueError("Sender daily limit must be positive.")
    candidate = next_allowed_send_time(sender, candidate)
    while True:
        local = candidate.astimezone(sender_timezone(sender))
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        start = day_start.astimezone(timezone.utc)
        end = (day_start + timedelta(days=1)).astimezone(timezone.utc)
        pending = select(func.count(EmailDraft.id)).where(
            EmailDraft.sender_account_id == sender.id, EmailDraft.status == "queued",
            EmailDraft.scheduled_at >= start, EmailDraft.scheduled_at < end,
        )
        if exclude_draft_id is not None:
            pending = pending.where(EmailDraft.id != exclude_draft_id)
        reserved = session.exec(pending).one()
        sent = session.exec(select(func.count(SendRecord.id)).where(
            SendRecord.sender_account_id == sender.id,
            SendRecord.sent_at >= start, SendRecord.sent_at < end,
        )).one()
        unresolved = session.exec(_unresolved_attempts(sender.id).where(
            EmailEvent.occurred_at >= start, EmailEvent.occurred_at < end)).all()
        if reserved + sent + len(unresolved) < sender.daily_limit:
            return candidate
        candidate = next_allowed_send_time(sender, end)


def _next_global_queue_slot(session: Session, sender: SenderAccount) -> datetime:
    """Reserve a queue slot after all queued or previously sent email.

    Sender-specific windows and limits still apply, but all sender accounts share
    one delivery timeline so that a multi-sender batch cannot burst together.
    """
    candidate = _next_sender_queue_slot(session, sender)
    delay = timedelta(seconds=sender_interval_seconds(sender))
    latest_queued_at = session.exec(
        select(EmailDraft.scheduled_at)
        .where(EmailDraft.status == "queued", EmailDraft.scheduled_at.is_not(None))
        .order_by(EmailDraft.scheduled_at.desc(), EmailDraft.id.desc())
    ).first()
    latest_sent_at = _last_global_sent_at(session)
    for previous_at in (latest_queued_at, latest_sent_at):
        if previous_at is not None:
            candidate = max(candidate, _as_aware_utc(previous_at) + delay)
    return reserve_sender_day_slot(session, sender, candidate)


def _reschedule_sender_queue_from(session: Session, sender: SenderAccount, start_at: datetime, event_type: str) -> int:
    schedule_at = start_at
    queued_drafts = _ordered_sender_queue(session, sender.id)
    for draft in queued_drafts:
        draft.scheduled_at = None
        session.add(draft)
    session.flush()
    for draft in queued_drafts:
        schedule_at = reserve_sender_day_slot(session, sender, schedule_at, exclude_draft_id=draft.id)
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
    scheduled_at = reserve_sender_day_slot(session, sender, scheduled_at, exclude_draft_id=draft.id)
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
    unresolved = session.exec(_unresolved_attempts().where(
        EmailDraft.template_snapshot.contains("feishu_native_reply")
    ).order_by(EmailEvent.occurred_at.desc())).first()
    return max((_as_aware_utc(item) for item in (value, unresolved) if item is not None), default=None)


def normalize_thread_subject(subject: str | None) -> str:
    """Normalize harmless display differences for duplicate-thread checks."""
    return " ".join((subject or "").split()).casefold()


def recent_duplicate_thread_send_records(
    session: Session,
    sender_id: int,
    recipient_email: str,
    subject: str,
    hours: int = RECENT_DUPLICATE_SEND_HOURS,
) -> list[SendRecord]:
    """Return successful sends for the same sender, recipient, and thread in the window."""
    recipient = (recipient_email or "").strip().casefold()
    normalized_subject = normalize_thread_subject(subject)
    if not recipient or not normalized_subject:
        return []
    cutoff = utc_now() - timedelta(hours=hours)
    candidates = session.exec(
        select(SendRecord)
        .where(
            SendRecord.sender_account_id == sender_id,
            func.lower(SendRecord.recipient_email) == recipient,
            func.lower(SendRecord.smtp_status) == "sent",
            SendRecord.sent_at >= cutoff,
        )
        .order_by(SendRecord.sent_at.desc())
    ).all()
    return [record for record in candidates if normalize_thread_subject(record.subject) == normalized_subject]


def queue_draft(
    session: Session,
    draft_id: int,
    sender_id: int,
    scheduled_at: datetime | None = None,
    commit: bool = True,
    allow_recent_duplicate: bool = False,
    refresh_before_queue: bool = True,
    audit_before_queue: bool = True,
) -> EmailDraft:
    draft = session.get(EmailDraft, draft_id)
    sender = session.get(SenderAccount, sender_id)
    if draft is None or sender is None:
        raise ValueError("Draft or sender not found.")
    if draft.status != "approved":
        raise ValueError("Draft must be marked handled before queuing.")
    if not sender.is_active:
        raise ValueError("Sender account is not active.")
    contact = session.get(Contact, draft.contact_id)
    if contact is None:
        raise ValueError("Draft contact not found.")
    if recent_duplicate_thread_send_records(session, sender.id, contact.email, draft.subject) and not allow_recent_duplicate:
        raise ValueError(
            "Same sender, recipient, and subject already have a successful send within the last 24 hours. "
            "Confirm the same-thread resend to continue."
        )
    existing_company_queue = session.exec(
        select(EmailDraft).where(
            EmailDraft.company_id == draft.company_id,
            EmailDraft.status == "queued",
            EmailDraft.id != draft.id,
        )
    ).first()
    if existing_company_queue:
        raise ValueError("Company already has a queued draft. One company can only have one draft in a send batch.")
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
    if refresh_before_queue:
        refresh_draft_from_current_template(session, draft, sender=sender)
    issues = audit_draft(session, draft) if audit_before_queue else []
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
    planned_at = scheduled_at or _next_global_queue_slot(session, sender)
    draft.sender_account_id = sender.id
    draft.status = "queued"
    draft.scheduled_at = planned_at
    draft.updated_at = utc_now()
    session.add(draft)
    session.add(EmailEvent(draft_id=draft.id, event_type="queued"))
    if commit:
        session.commit()
        session.refresh(draft)
    return draft


def _company_has_successful_template(session: Session, company_id: str, template_id: int) -> bool:
    return session.exec(
        select(SendRecord).where(
            SendRecord.company_id == company_id,
            SendRecord.template_id == template_id,
            func.lower(SendRecord.smtp_status) == "sent",
        )
    ).first() is not None


def _handoff_exclude(session: Session, draft: EmailDraft, reason: str) -> None:
    draft.status = "pending_review"
    draft.sender_account_id = None
    draft.scheduled_at = None
    draft.approved_at = None
    draft.error_message = reason
    draft.updated_at = utc_now()
    session.add(draft)
    session.add(EmailEvent(draft_id=draft.id, event_type="queue_handoff_excluded", metadata_json=reason))


def create_queue_handoff(
    session: Session,
    draft_ids: list[int],
    sender_ids: list[int],
    template_id: int | None,
    send_mode: str,
    *,
    allow_recent_duplicate: bool = False,
) -> QueueHandoffRun:
    """Persist a one-click intake request; the scheduler consumes 50 rows at a time."""
    normalized_draft_ids = list(dict.fromkeys(draft_id for draft_id in draft_ids if draft_id))
    normalized_sender_ids = list(dict.fromkeys(sender_id for sender_id in sender_ids if sender_id))
    if not normalized_draft_ids or not normalized_sender_ids:
        raise ValueError("A queue handoff needs drafts and at least one sender.")
    if template_id is not None and session.get(EmailTemplate, template_id) is None:
        raise ValueError("Selected send template not found.")
    active = session.exec(
        select(QueueHandoffRun).where(QueueHandoffRun.status.in_(("pending", "running")))
    ).first()
    if active:
        raise ValueError("Another queue handoff is already importing. Check its progress before starting a new one.")
    handoff = QueueHandoffRun(
        draft_ids_json=json.dumps(normalized_draft_ids),
        sender_ids_json=json.dumps(normalized_sender_ids),
        template_id=template_id,
        send_mode="rotate" if send_mode == "rotate" else "single",
        allow_recent_duplicate=allow_recent_duplicate,
        batch_size=QUEUE_HANDOFF_BATCH_SIZE,
        total_count=len(normalized_draft_ids),
    )
    session.add(handoff)
    # Preserve the existing start-send behaviour: the first imported chunk can
    # proceed under the normal native queue scheduler without a second click.
    set_app_setting(session, "queue_paused", "false", commit=False)
    session.commit()
    session.refresh(handoff)
    return handoff


def process_next_queue_handoff_batch(session: Session, handoff_id: str | None = None) -> dict[str, int | bool | str]:
    """Import exactly one durable handoff chunk. Never retry an unknown write."""
    if not QUEUE_PROCESSING_LOCK.acquire(blocking=False):
        return {"queued": 0, "excluded": 0, "processed": 0, "busy": True}
    try:
        handoff = session.get(QueueHandoffRun, handoff_id) if handoff_id else session.exec(
            select(QueueHandoffRun)
            .where(QueueHandoffRun.status.in_(("pending", "running")))
            .order_by(QueueHandoffRun.created_at, QueueHandoffRun.id)
        ).first()
        if handoff is None:
            return {"queued": 0, "excluded": 0, "processed": 0, "idle": True}
        handoff_id = handoff.id
        if handoff.status not in {"pending", "running"}:
            return {"queued": 0, "excluded": 0, "processed": 0, "status": handoff.status}
        if is_queue_paused(session):
            return {"queued": 0, "excluded": 0, "processed": 0, "paused": True}
        draft_ids = json.loads(handoff.draft_ids_json)
        sender_ids = json.loads(handoff.sender_ids_json)
        start = handoff.cursor
        chunk = draft_ids[start : start + max(1, handoff.batch_size)]
        if not chunk:
            handoff.status = "completed_with_exclusions" if handoff.excluded_count else "completed"
            handoff.updated_at = utc_now()
            session.add(handoff)
            session.commit()
            return {"queued": 0, "excluded": 0, "processed": 0, "status": handoff.status}
        selected_template = session.get(EmailTemplate, handoff.template_id) if handoff.template_id is not None else None
        queued = 0
        excluded = 0
        for offset, draft_id in enumerate(chunk):
            draft = session.get(EmailDraft, draft_id)
            sender_id = sender_ids[(start + offset) % len(sender_ids)]
            sender = session.get(SenderAccount, sender_id)
            if draft is None or sender is None or not sender.is_active:
                excluded += 1
                continue
            if draft.status != "approved":
                # Idempotent stale submissions must not alter queued or terminal rows.
                excluded += 1
                continue
            contact = session.get(Contact, draft.contact_id)
            if contact is None or contact.status != "active" or contact.company_id != draft.company_id:
                _handoff_exclude(session, draft, "Approved recipient is unavailable or belongs to another company.")
                excluded += 1
                continue
            company = session.get(Company, draft.company_id)
            if company is None or _company_blocks_draft(company, draft):
                _handoff_exclude(session, draft, "Company is in NO-GO list or no longer exists.")
                excluded += 1
                continue
            # With no selector choice, the draft's own approved template is
            # still the effective template and follows the same history rule.
            effective_template = selected_template or (
                session.get(EmailTemplate, draft.template_id) if draft.template_id is not None else None
            )
            if effective_template is not None and effective_template.template_type == "first_touch" and _company_has_successful_template(
                session, draft.company_id, effective_template.id
            ):
                _handoff_exclude(session, draft, "Selected template was already successfully used for this company.")
                excluded += 1
                continue
            try:
                if effective_template is not None and not is_native_reply_draft(draft.template_snapshot):
                    refresh_draft_from_template(session, draft, effective_template, sender=sender)
                # This is deliberately the only CC sanitation point. Send-time
                # audit remains a hard gate for To/NO-GO, but does not mutate CC.
                sanitize_cc_once(session, draft)
                issues = audit_draft(session, draft)
                if issues:
                    _handoff_exclude(session, draft, "; ".join(issues))
                    excluded += 1
                    continue
                draft.status = "approved"
                draft.approved_at = utc_now()
                draft.error_message = None
                session.add(draft)
                queue_draft(
                    session,
                    draft.id,
                    sender.id,
                    commit=False,
                    allow_recent_duplicate=handoff.allow_recent_duplicate,
                    refresh_before_queue=False,
                    audit_before_queue=False,
                )
                queued += 1
            except (ValueError, RuntimeError) as exc:
                _handoff_exclude(session, draft, str(exc))
                excluded += 1
        handoff.cursor = start + len(chunk)
        handoff.queued_count += queued
        handoff.excluded_count += excluded
        handoff.status = "running" if handoff.cursor < handoff.total_count else (
            "completed_with_exclusions" if handoff.excluded_count else "completed"
        )
        handoff.updated_at = utc_now()
        session.add(handoff)
        session.commit()
        session.refresh(handoff)
        return {
            "queued": queued,
            "excluded": excluded,
            "processed": len(chunk),
            "status": handoff.status,
            "cursor": handoff.cursor,
            "total": handoff.total_count,
        }
    except Exception as exc:
        session.rollback()
        if handoff_id:
            failed_handoff = session.get(QueueHandoffRun, handoff_id)
            if failed_handoff:
                failed_handoff.last_error = str(exc)
                failed_handoff.status = "failed"
                failed_handoff.updated_at = utc_now()
                session.add(failed_handoff)
                session.commit()
        raise
    finally:
        QUEUE_PROCESSING_LOCK.release()


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
    draft.cc_emails = template.cc_emails if template.cc_enabled else None
    draft.template_snapshot = snapshot
    draft.updated_at = utc_now()
    session.add(draft)
    session.add(EmailEvent(draft_id=draft.id, event_type="template_refreshed_before_send"))


def refresh_draft_from_current_template(session: Session, draft: EmailDraft, sender: SenderAccount | None = None) -> None:
    if draft.template_id is None or is_native_reply_draft(draft.template_snapshot):
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
    """Run one process-wide, globally serialized outbound queue pass."""
    if not QUEUE_PROCESSING_LOCK.acquire(blocking=False):
        return {"processed": 0, "sent": 0, "failed": 0, "skipped": 0, "deferred": 0, "paused": False, "busy": True}
    try:
        return _process_due_queue(session, limit=limit, draft_ids=draft_ids)
    finally:
        QUEUE_PROCESSING_LOCK.release()


def _process_due_queue(session: Session, limit: int = 10, draft_ids: list[int] | None = None) -> dict[str, int | bool]:
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
                audit_issues = audit_draft(session, draft)
            except Exception as exc:
                audit_issues = [f"Pre-send audit failed: {exc}"]
            if audit_issues:
                draft.status = "pending_review"
                draft.sender_account_id = None
                draft.scheduled_at = None
                draft.error_message = "; ".join(audit_issues)
                session.add(EmailEvent(draft_id=draft.id, event_type="pre_send_audit_blocked", metadata_json=draft.error_message))
                failed += 1
            elif not _inside_sender_window(sender, utc_now()):
                draft.scheduled_at = reserve_sender_day_slot(session, sender, next_window_start(sender), exclude_draft_id=draft.id)
            elif daily_capacity_count(session, sender) >= sender.daily_limit:
                if sender.id not in deferred_sender_ids:
                    deferred += _reschedule_sender_queue_from(
                        session,
                        sender,
                        next_daily_window_start(sender),
                        "daily_limit_deferred",
                    )
                    deferred_sender_ids.add(sender.id)
            elif _active_suppression_for_email(session, contact.email):
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
                            session.commit()
                            continue
                last_sender_sent_at = _last_sent_at(session, sender.id)
                last_global_sent_at = _last_global_sent_at(session)
                previous_send_at = max(
                    (value for value in (last_sender_sent_at, last_global_sent_at) if value is not None),
                    default=None,
                )
                if previous_send_at is not None:
                    # Queue scheduling is global, and this is the durable send-time
                    # guard for overdue work or drafts introduced by older versions.
                    next_allowed_at = next_allowed_send_time(
                        sender,
                        _as_aware_utc(previous_send_at) + timedelta(seconds=sender_interval_seconds(sender)),
                    )
                    if utc_now() < next_allowed_at:
                        _append_draft_to_sender_queue_tail(
                            session,
                            sender,
                            draft,
                            next_allowed_at,
                        )
                        session.commit()
                        continue
                draft.status = "sending"
                draft.error_message = "Delivery attempt started; verify outcome before retrying if interrupted."
                session.add(draft)
                session.add(EmailEvent(draft_id=draft.id, event_type="send_attempt_started", occurred_at=utc_now(),
                    metadata_json=json.dumps({"recipient_email": contact.email, "sender_email": sender.email})))
                session.commit()
                delivery_accepted = False
                try:
                    native_reply_result = None
                    if get_settings().dry_run_email:
                        send_result = "simulated"
                    elif is_native_reply:
                        native_reply_result = send_queued_native_reply(draft, sender, contact)
                        send_result = "sent"
                    else:
                        send_result = send_draft(session, draft, sender)
                    delivery_accepted = send_result == "sent"
                    now = utc_now()
                    draft.status = "sent" if send_result == "sent" else "simulated"
                    draft.sent_at = now
                    draft.error_message = None
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
                            event_type="feishu_native_reply_sent" if is_native_reply and send_result == "sent" else draft.status,
                            metadata_json=json.dumps(native_reply_result, ensure_ascii=False) if native_reply_result else None,
                        )
                    )
                    if send_result == "sent":
                        send_record = SendRecord(
                                draft_id=draft.id,
                                company_id=draft.company_id,
                                domain=company.domain if company else "",
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
                                activity_date=now,
                                sent_at=now,
                                recipient_domain=contact.email.rsplit("@", 1)[-1].casefold(),
                                raw_json=json.dumps(
                                {"draft_id": draft.id, "tracking_id": draft.tracking_id},
                                ensure_ascii=False,
                            ),
                        )
                        session.add(send_record)
                        session.flush()
                        send_record.source_record_id = str(send_record.id)
                        send_record.activity_key = f"bd_email_workbench:sendrecord:{send_record.id}"
                        session.add(send_record)
                        if company:
                            company.activity_record_count = (company.activity_record_count or 0) + 1
                            session.add(company)
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
                except DeliveryUncertainError as exc:
                    draft.status = "send_unknown"
                    draft.error_message = str(exc)
                    session.add(EmailEvent(draft_id=draft.id, event_type="delivery_requires_verification",
                        metadata_json=json.dumps(exc.receipt, ensure_ascii=False)))
                    if is_native_reply:
                        snapshot = native_reply_metadata(draft.template_snapshot)
                        snapshot["delivery_result"] = exc.receipt
                        draft.template_snapshot = json.dumps(snapshot, ensure_ascii=False)
                    failed += 1
                except Exception as exc:
                    draft.status = "send_unknown" if delivery_accepted else "failed"
                    draft.error_message = str(exc)
                    if delivery_accepted:
                        session.add(EmailEvent(draft_id=draft.id, event_type="delivery_requires_verification",
                            metadata_json=json.dumps({"external_accepted": True, "local_error": str(exc)})))
                    failed += 1
        draft.updated_at = utc_now()
        session.add(draft)
        session.commit()
    return {"processed": len(due), "sent": sent, "failed": failed, "skipped": skipped, "deferred": deferred, "paused": False}


def scan_followups(session: Session) -> dict[str, int]:
    staged_result = scan_staged_followups(session)
    # Fixed-delay FollowUpRule rows are retired: the dual-counter cadence is the
    # only automated route.  A disabled cadence therefore creates nothing.
    return staged_result or {"followup_drafts": 0}
