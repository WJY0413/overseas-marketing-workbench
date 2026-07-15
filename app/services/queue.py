import random
import re
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Company, Contact, EmailDraft, EmailEvent, EmailTemplate, FollowUpRule, SendRecord, SenderAccount, Suppression
from app.services.email_quality import validate_contact_email
from app.services.app_settings import is_queue_paused
from app.services.mailer import send_draft
from app.services.mailer import validate_attachment_paths
from app.services.queue_state import reconcile_queue_state
from app.services.templates import render_template
from app.time_utils import local_day_bounds_utc, utc_now

UNRESOLVED_VARIABLE_RE = re.compile(r"{{\s*[^{}]+\s*}}")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_SEND_INTERVAL_SECONDS = 30
SHARED_RECIPIENT_DOMAINS = {
    "aol.com", "btconnect.com", "gmail.com", "googlemail.com", "hotmail.com", "hotmail.co.uk",
    "icloud.com", "live.com", "outlook.com", "yahoo.com",
}


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
    issues: list[str] = []
    unresolved = sorted(set(UNRESOLVED_VARIABLE_RE.findall(f"{draft.subject}\n{draft.body_html}\n{draft.body_text or ''}")))
    if unresolved:
        issues.append(f"Template variables not rendered: {', '.join(unresolved)}")
    if contact is None:
        issues.append("Contact not found.")
    else:
        email_check = validate_contact_email(contact.email.strip(), check_deliverability=True)
        if email_check.is_blocking:
            issues.append(f"Recipient email quality check failed: {email_check.reason}")
        elif not EMAIL_RE.match(contact.email.strip()):
            issues.append(f"Invalid recipient email: {contact.email}")
        elif session.exec(select(Suppression).where(Suppression.email == contact.email.strip().lower())).first():
            issues.append(f"Recipient is in suppression list: {contact.email}")
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


def daily_sent_count(session: Session, sender_id: int) -> int:
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
    timezone = ZoneInfo(get_settings().app_timezone)
    local_now = now.astimezone(timezone)
    if _is_time_inside_window(local_now.time(), sender.window_start, sender.window_end):
        return now
    start_today = datetime.combine(local_now.date(), sender.window_start, tzinfo=timezone)
    if local_now.time() < sender.window_start:
        return start_today.astimezone(ZoneInfo("UTC"))
    return (start_today + timedelta(days=1)).astimezone(ZoneInfo("UTC"))


def next_daily_window_start(sender: SenderAccount, now: datetime | None = None) -> datetime:
    now = now or utc_now()
    timezone = ZoneInfo(get_settings().app_timezone)
    local_now = now.astimezone(timezone)
    start_next_day = datetime.combine(local_now.date() + timedelta(days=1), sender.window_start, tzinfo=timezone)
    return start_next_day.astimezone(ZoneInfo("UTC"))


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
    if daily_sent_count(session, sender.id) >= sender.daily_limit:
        candidate = next_daily_window_start(sender)

    latest = session.exec(
        select(EmailDraft)
        .where(EmailDraft.status == "queued", EmailDraft.sender_account_id == sender.id)
        .order_by(EmailDraft.scheduled_at.desc(), EmailDraft.id.desc())
    ).first()
    if latest and latest.scheduled_at:
        latest_at = _as_aware_utc(latest.scheduled_at)
        if latest_at >= candidate:
            return latest_at + timedelta(seconds=sender_interval_seconds(sender))
    return candidate


def _reschedule_sender_queue_from(session: Session, sender: SenderAccount, start_at: datetime, event_type: str) -> int:
    schedule_at = start_at
    queued_drafts = _ordered_sender_queue(session, sender.id)
    for draft in queued_drafts:
        draft.scheduled_at = schedule_at
        draft.updated_at = utc_now()
        session.add(draft)
        session.add(EmailEvent(draft_id=draft.id, event_type=event_type))
        schedule_at = schedule_at + timedelta(seconds=sender_interval_seconds(sender))
    return len(queued_drafts)


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
    draft.template_id = template.id
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
    local = now.astimezone(ZoneInfo(get_settings().app_timezone)).time()
    return _is_time_inside_window(local, sender.window_start, sender.window_end)


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
            elif daily_sent_count(session, sender.id) >= sender.daily_limit:
                if sender.id not in deferred_sender_ids:
                    deferred += _reschedule_sender_queue_from(
                        session,
                        sender,
                        next_daily_window_start(sender),
                        "daily_limit_deferred",
                    )
                    deferred_sender_ids.add(sender.id)
            elif session.exec(select(Suppression).where(Suppression.email == contact.email.lower())).first():
                draft.status = "skipped"
                draft.error_message = "Suppressed email."
                skipped += 1
            else:
                last_sent_at = _last_sent_at(session, sender.id)
                if last_sent_at is not None:
                    next_allowed_at = last_sent_at + timedelta(seconds=MIN_SEND_INTERVAL_SECONDS)
                    if utc_now() < next_allowed_at:
                        draft.scheduled_at = next_allowed_at
                        draft.updated_at = utc_now()
                        session.add(draft)
                        continue
                try:
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
                    session.add(EmailEvent(draft_id=draft.id, event_type=draft.status))
                    if send_result == "sent":
                        session.add(
                            SendRecord(
                                draft_id=draft.id,
                                company_id=draft.company_id,
                                contact_id=draft.contact_id,
                                sender_account_id=sender.id,
                                sender_email=sender.email,
                                recipient_email=contact.email,
                                subject=draft.subject,
                                body_html=draft.body_html,
                                body_text=draft.body_text,
                                cc_emails=draft.cc_emails,
                                attachment_paths=draft.attachment_paths,
                                smtp_status="sent",
                                sent_at=now,
                            )
                        )
                        sent += 1
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
    generated = 0
    rules = session.exec(select(FollowUpRule).where(FollowUpRule.is_active == True)).all()
    for rule in rules:
        template = session.get(EmailTemplate, rule.template_id)
        if template is None:
            continue
        cutoff = utc_now() - timedelta(days=rule.delay_days)
        sent_drafts = session.exec(
            select(EmailDraft).where(
                EmailDraft.status == "sent",
                EmailDraft.sent_at <= cutoff,
            )
        ).all()
        for sent_draft in sent_drafts:
            company = session.get(Company, sent_draft.company_id)
            contact = session.get(Contact, sent_draft.contact_id)
            if not company or not contact:
                continue
            if company.status in {"replied", "unsubscribed", "paused", "blacklisted"}:
                continue
            if company.priority not in {item.strip() for item in rule.priorities.split(",")}:
                continue
            existing = session.exec(
                select(EmailDraft).where(
                    EmailDraft.company_id == company.id,
                    EmailDraft.contact_id == contact.id,
                    EmailDraft.follow_up_step == sent_draft.follow_up_step + 1,
                    EmailDraft.status.in_(["pending_review", "approved", "queued"]),
                )
            ).first()
            if existing:
                continue
            try:
                subject, html, text, snapshot = render_template(template, company, contact, session=session)
                render_error = None
            except Exception as exc:
                subject = template.subject
                html = template.body_html
                text = template.body_text
                snapshot = json.dumps({"template_id": template.id, "render_error": str(exc)}, ensure_ascii=False)
                render_error = f"Template render error: {exc}"
            draft = EmailDraft(
                company_id=company.id,
                contact_id=contact.id,
                template_id=template.id,
                subject=subject,
                body_html=html,
                body_text=text,
                cc_emails=template.cc_emails if template.cc_enabled else None,
                follow_up_step=sent_draft.follow_up_step + 1,
                template_snapshot=snapshot,
            )
            session.add(draft)
            session.flush()
            if render_error:
                draft.status = "pending_review"
                draft.error_message = render_error
                draft.updated_at = utc_now()
                session.add(draft)
            else:
                apply_audit_result(session, draft)
            company.status = "follow_up_due"
            company.next_follow_up_at = utc_now()
            company.updated_at = utc_now()
            session.add(company)
            generated += 1
    session.commit()
    return {"followup_drafts": generated}
