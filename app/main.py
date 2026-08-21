import json
import logging
import re
from base64 import b64decode
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func
from sqlmodel import Session, select

from app.config import get_settings
from app.db import create_db_and_tables, engine, get_session, is_unified_database
from app.models import BounceRecord, Company, Contact, ContactRoute, DraftPrepItem, EmailDraft, EmailEvent, EmailTemplate, FollowUpRule, QueueHandoffRun, SendRecord, SenderAccount, Suppression, TemplateRotationPolicy
from app.scheduler import scheduler, start_scheduler, stop_scheduler
from app.seed import seed_defaults
from app.services.docx_templates import parse_docx_email_template, parse_docx_signature_html
from app.services.importer import import_bd_json_candidates, import_contacts, is_generic_email
from app.services.no_go_companies import (
    import_no_go_companies,
    is_no_go_company,
    parse_no_go_company_file,
)
from app.services.email_quality import validate_contact_email
from app.services.app_settings import is_queue_paused, set_app_setting
from app.services.bd_master_sync import get_bd_master_sync_status, run_bd_master_sync
from app.services.followup_cadence import generate_dual_counter_followups, get_followup_cadence, save_followup_cadence
from app.services.suppression import suppression_matches_email
from app.services.queue import (
    audit_draft,
    apply_audit_result,
    approve_draft,
    cancel_queued_draft,
    create_queue_handoff,
    daily_sent_count,
    format_sender_windows,
    next_window_start,
    next_allowed_send_time,
    parse_sender_windows_text,
    process_due_queue,
    queue_draft,
    recipient_company_domain,
    recent_duplicate_thread_send_records,
    refresh_draft_from_template,
    refresh_pending_audits,
    scan_followups,
    sender_interval_seconds,
    sender_windows,
    sender_timezone,
    validate_sender_timezone,
)
from app.services.reports import export_sending_report
from app.services.bounce_scanner import bounce_log_path, scan_bounces
from app.services.batch_planner import build_batch_plan, ensure_rotation_policies, ensure_rotation_policy, update_rotation_policy
from app.services.queue_state import reconcile_queue_state
from app.services.power_awake import release_system_awake, sync_power_awake
from app.services.sending_limits import DEFAULT_RECOMMENDED_DAILY_LIMIT, get_sending_limit_advice
from app.services.secrets import encrypt_secret
from app.services.mailer import parse_attachment_paths, serialize_attachment_paths
from app.services.signatures import (
    DEFAULT_SIGNATURE_TEMPLATE_NAME,
    SIGNATURE_TEMPLATE_TYPE,
    country_rules_to_text,
    get_signature_config,
    get_signature_template,
    is_signature_template,
    mail_template_filter,
    parse_country_rules_text,
    parse_sender_rules_text,
    render_signature_html,
    save_signature_config,
    sender_rules_to_text,
    signature_context,
    upsert_signature_template,
)
from app.services.render_validation import TemplateFieldResolutionError, validate_template_source
from app.services.templates import render_template
from app.services.linkedin_connections import LinkedInConnectionError, confirm as confirm_linkedin_connections, list_candidates as linkedin_candidates, recent_workbench_confirmations, restore_mistag, today_metrics as linkedin_today_metrics
from app.time_utils import display_dt, local_now, utc_now
from app.version import APP_VERSION

app = FastAPI(title="BD Email Workbench Lite")
logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
PIXEL = b64decode("R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==")
DRAFT_GENERATE_BATCH_SIZE = 50
CRM_CANDIDATE_PAGE_SIZE = 10
PLACEHOLDER_SENDER_EMAIL = "your.name@example.com"


def attachment_paths_text(value: str | None) -> str:
    return "\n".join(parse_attachment_paths(value))


templates.env.globals["attachment_paths_text"] = attachment_paths_text


def _clear_startup_workspace(session: Session) -> None:
    session.exec(delete(DraftPrepItem))
    # EmailDraft is durable review/queue evidence.  A Workbench restart must
    # never erase a prepared batch before the native handoff can process it.


@app.on_event("startup")
def startup() -> None:
    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.reports_dir.mkdir(parents=True, exist_ok=True)
    create_db_and_tables()
    with Session(engine) as session:
        _clear_startup_workspace(session)
        session.commit()
    seed_defaults()
    with Session(engine) as session:
        # Existing templates receive one durable policy at migration time.  The
        # planner itself never writes or falls back to a template-ID whitelist.
        # Startup-gate unit tests intentionally replace Session with a small
        # lifecycle double; policy migration belongs only to a real DB session.
        if hasattr(session, "exec"):
            ensure_rotation_policies(session)
        session.commit()
    if is_unified_database() or settings.bd_database_path is None:
        sync_result = {
            "status": "local_unified_database",
            "run_id": None,
            "error": None,
            "scheduler_safe": True,
        }
    else:
        try:
            sync_result = run_bd_master_sync(engine, settings.bd_database_path)
        except Exception as exc:
            logger.exception("BD master startup sync raised before it could persist a failed audit run.")
            sync_result = {
                "status": "failed",
                "run_id": None,
                "error": str(exc),
                "scheduler_safe": False,
            }
    if sync_result["scheduler_safe"]:
        with Session(engine) as session:
            sync_power_awake(session)
        start_scheduler()
    else:
        stop_scheduler()
        logger.error(
            "BD master startup sync blocked the scheduler: status=%s run_id=%s error=%s",
            sync_result.get("status"),
            sync_result.get("run_id"),
            sync_result.get("error"),
        )


@app.on_event("shutdown")
def shutdown() -> None:
    stop_scheduler()
    release_system_awake()


def _redirect(message: str) -> RedirectResponse:
    return RedirectResponse(f"/?message={message}", status_code=303)


def _redirect_to(path: str, message: str) -> RedirectResponse:
    separator = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{separator}message={message}", status_code=303)


def _update_env_value(key: str, value: str, env_path: Path = Path(".env")) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    updated = False
    new_lines: list[str] = []
    for line in lines:
        if line.strip().startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        current_key, _ = line.split("=", 1)
        if current_key.strip() == key:
            new_lines.append(f"{key}={value}")
            updated = True
        else:
            new_lines.append(line)
    if not updated:
        new_lines.append(f"{key}={value}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    get_settings.cache_clear()


def _parse_sender_window(window_start: str, window_end: str):
    def parse_time(value: str):
        value = value.strip()
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(value, fmt).time()
            except ValueError:
                continue
        raise ValueError("Send window must use HH:MM format.")

    try:
        return parse_time(window_start), parse_time(window_end)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc


def _parse_delay_settings(
    interval_mode: str,
    random_delay_min_seconds: str,
    random_delay_max_seconds: str,
    fixed_delay_seconds: str,
) -> tuple[int, int]:
    if interval_mode == "none":
        return 0, 0
    if interval_mode == "fixed":
        fixed = int(fixed_delay_seconds or "0")
        if fixed < 0:
            raise ValueError("Fixed delay cannot be negative.")
        return fixed, fixed
    minimum = int(random_delay_min_seconds or "180")
    maximum = int(random_delay_max_seconds or "600")
    if minimum < 0 or maximum < minimum:
        raise ValueError("Random delay range is invalid.")
    return minimum, maximum


def _normalize_template_body(body_html: str) -> str:
    body = body_html.strip()
    if not body:
        return ""
    if re.search(r"<[a-zA-Z][\s\S]*>", body):
        return body
    paragraphs = [
        f"<p>{escape(paragraph).replace(chr(10), '<br>')}</p>"
        for paragraph in re.split(r"\n\s*\n", body)
        if paragraph.strip()
    ]
    return "\n".join(paragraphs) or f"<p>{escape(body)}</p>"


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _template_name_from_upload(filename: str | None) -> str:
    return Path(filename or "").stem.strip()


def _signature_form(config: dict) -> dict:
    return {
        **config,
        "country_rules_text": country_rules_to_text(config),
        "sender_rules_text": sender_rules_to_text(config),
    }


def _deactivate_same_name_templates(session: Session, template_id: int | None, name: str) -> None:
    duplicates = session.exec(select(EmailTemplate).where(EmailTemplate.name == name.strip())).all()
    for duplicate in duplicates:
        if duplicate.id != template_id:
            duplicate.is_active = False
            duplicate.updated_at = utc_now()
            session.add(duplicate)


def _create_mail_template_version(
    session: Session,
    *,
    name: str,
    template_type: str,
    subject: str,
    body_html: str,
    body_text: str | None,
    cc_enabled: bool,
    cc_emails: str | None,
    is_active: bool = True,
    replaces_template_id: int | None = None,
) -> EmailTemplate:
    validate_template_source(
        subject=subject.strip(),
        body_html=body_html,
        body_text=body_text,
    )
    template = EmailTemplate(
        name=name.strip(),
        template_type=template_type,
        subject=subject.strip(),
        body_html=body_html,
        body_text=body_text or None,
        cc_enabled=cc_enabled,
        cc_emails=cc_emails.strip() if cc_emails and cc_emails.strip() else None,
        is_active=is_active,
    )
    session.add(template)
    session.flush()
    _deactivate_same_name_templates(session, template.id, template.name)
    if template.template_type == "first_touch":
        ensure_rotation_policy(session, template, replaces_template_id=replaces_template_id)
    if replaces_template_id is not None and is_active:
        rules = session.exec(
            select(FollowUpRule).where(FollowUpRule.template_id == replaces_template_id)
        ).all()
        for rule in rules:
            rule.template_id = template.id
            rule.updated_at = utc_now()
            session.add(rule)
    return template


def _dashboard_stats(session: Session) -> dict[str, int]:
    senders = session.exec(select(SenderAccount).where(SenderAccount.email != PLACEHOLDER_SENDER_EMAIL)).all()
    return {
        "sent_today": sum(daily_sent_count(session, sender) for sender in senders),
        "queued": session.exec(select(func.count(EmailDraft.id)).where(EmailDraft.status == "queued")).one(),
        "approved": session.exec(select(func.count(EmailDraft.id)).where(EmailDraft.status == "approved")).one(),
        "audit_exceptions": session.exec(
            select(func.count(EmailDraft.id)).where(EmailDraft.status == "pending_review")
        ).one(),
        "failed": session.exec(select(func.count(EmailDraft.id)).where(EmailDraft.status == "failed")).one(),
        "skipped": session.exec(select(func.count(EmailDraft.id)).where(EmailDraft.status == "skipped")).one(),
        "simulated": session.exec(select(func.count(EmailDraft.id)).where(EmailDraft.status == "simulated")).one(),
        "followups_due": session.exec(
            select(func.count(Company.id)).where(Company.next_follow_up_at != None)
        ).one(),
        "opens": session.exec(select(func.count(EmailEvent.id)).where(EmailEvent.event_type == "open")).one(),
        "bounces": session.exec(select(func.count(BounceRecord.id))).one(),
    }


def _sender_usage(session: Session) -> list[dict]:
    senders = session.exec(
        select(SenderAccount)
        .where(SenderAccount.email != PLACEHOLDER_SENDER_EMAIL)
        .order_by(SenderAccount.email)
    ).all()
    return [
        {
            "sender": sender,
            "sent_today": daily_sent_count(session, sender),
            "remaining": max(sender.daily_limit - daily_sent_count(session, sender), 0),
            "advice": get_sending_limit_advice(sender),
            "send_timezone": sender_timezone(sender).key,
            "send_windows": format_sender_windows(sender),
        }
        for sender in senders
    ]


def _sort_queue_drafts(
    drafts: list[EmailDraft],
    company_by_id: dict[int, Company],
    contact_by_id: dict[int, Contact],
    sort_by: str,
    sort_dir: str,
) -> list[EmailDraft]:
    status_rank = {"sent": 0, "simulated": 1, "queued": 2, "failed": 3, "skipped": 4}
    reverse = sort_dir == "desc"

    def dt_value(value):
        return value or datetime.max

    def key(draft: EmailDraft):
        company = company_by_id.get(draft.company_id)
        contact = contact_by_id.get(draft.contact_id)
        if sort_by == "scheduled_at":
            return (dt_value(draft.scheduled_at), status_rank.get(draft.status, 99))
        if sort_by == "sent_at":
            return (dt_value(draft.sent_at), status_rank.get(draft.status, 99))
        if sort_by == "company":
            return ((company.name if company else "").lower(), dt_value(draft.scheduled_at))
        if sort_by == "contact":
            return ((contact.full_name if contact else "").lower(), dt_value(draft.scheduled_at))
        return (status_rank.get(draft.status, 99), dt_value(draft.scheduled_at), dt_value(draft.sent_at))

    return sorted(drafts, key=key, reverse=reverse)


def _sort_send_records(
    records: list[SendRecord],
    company_by_id: dict[int, Company],
    contact_by_id: dict[int, Contact],
    sort_by: str,
    sort_dir: str,
) -> list[SendRecord]:
    reverse = sort_dir != "asc"

    def key(record: SendRecord):
        company = company_by_id.get(record.company_id)
        contact = contact_by_id.get(record.contact_id)
        if sort_by == "company":
            return ((company.name if company else "").lower(), record.sent_at)
        if sort_by == "contact":
            return ((contact.full_name if contact else "").lower(), record.sent_at)
        return record.sent_at

    return sorted(records, key=key, reverse=reverse)


def _reschedule_sender_queue(session: Session, sender: SenderAccount) -> int:
    queued_drafts = session.exec(
        select(EmailDraft)
        .where(EmailDraft.status == "queued", EmailDraft.sender_account_id == sender.id)
        .order_by(EmailDraft.scheduled_at)
    ).all()
    if not queued_drafts:
        return 0
    schedule_at = next_window_start(sender)
    for draft in queued_drafts:
        draft.scheduled_at = schedule_at
        draft.updated_at = utc_now()
        session.add(draft)
        session.add(EmailEvent(draft_id=draft.id, event_type="sender_window_rescheduled"))
        schedule_at = next_allowed_send_time(sender, schedule_at + timedelta(seconds=sender_interval_seconds(sender)))
    return len(queued_drafts)


def _set_draft_prep_contacts(session: Session, contact_ids: list[int]) -> int:
    session.exec(delete(DraftPrepItem))
    added = 0
    seen: set[int] = set()
    for contact_id in contact_ids:
        if contact_id in seen:
            continue
        seen.add(contact_id)
        contact = session.get(Contact, contact_id)
        if contact is None or contact.status != "active":
            continue
        session.add(DraftPrepItem(contact_id=contact_id))
        added += 1
    session.commit()
    return added


def _add_draft_prep_contacts(session: Session, contact_ids: list[int]) -> int:
    added = 0
    seen: set[int] = set()
    for contact_id in contact_ids:
        if contact_id in seen:
            continue
        seen.add(contact_id)
        contact = session.get(Contact, contact_id)
        if contact is None or contact.status != "active":
            continue
        if session.exec(select(DraftPrepItem).where(DraftPrepItem.contact_id == contact_id)).first():
            continue
        session.add(DraftPrepItem(contact_id=contact_id))
        added += 1
    session.commit()
    return added


def _contact_send_rank(contact: Contact) -> tuple[int, int, int]:
    rank = contact.priority_contact_rank if contact.priority_contact_rank is not None else 9999
    generic_penalty = 1 if _is_cc_contact(contact) else 0
    primary_penalty = 0 if contact.is_primary else 1
    return (rank, generic_penalty, primary_penalty)


def _is_cc_contact(contact: Contact) -> bool:
    label = f"{contact.full_name or ''} {contact.position or ''}".lower()
    return is_generic_email(contact.email or "") or "team" in label or "general inbox" in label or "public inbox" in label or " cc" in f" {label}"


def _preferred_contact_for_company(contacts: list[Contact]) -> Contact:
    return sorted(contacts, key=lambda contact: (*_contact_send_rank(contact), contact.id or 0))[0]


def _company_cc_emails(session: Session, company_id: int, primary_contact: Contact) -> str | None:
    contacts = session.exec(select(Contact).where(Contact.company_id == company_id, Contact.status == "active")).all()
    contact_ids = [contact.id for contact in contacts if contact.id is not None]
    emails: list[str] = []
    if contact_ids:
        routes = session.exec(
            select(ContactRoute).where(
                ContactRoute.contact_id.in_(contact_ids),
                ContactRoute.route_type == "email",
                ContactRoute.status == "active",
            )
        ).all()
        contact_by_id = {contact.id: contact for contact in contacts}
        for route in routes:
            email = (route.route_value or "").strip().lower()
            route_contact = contact_by_id.get(route.contact_id)
            if email and email != primary_contact.email.lower() and (is_generic_email(email) or (route_contact is not None and _is_cc_contact(route_contact))) and email not in emails:
                emails.append(email)
    for contact in contacts:
        email = (contact.email or "").strip().lower()
        if email and email != primary_contact.email.lower() and _is_cc_contact(contact) and email not in emails:
            emails.append(email)
    return emails[0] if emails else None


def _generate_drafts_for_contacts(session: Session, template: EmailTemplate, contact_ids: list[int]) -> dict[str, int]:
    stats = {
        "processed": 0,
        "created": 0,
        "updated": 0,
        "auto_approved": 0,
        "exceptions": 0,
        "skipped": 0,
        "consumed_prep": 0,
    }

    selected_contacts_by_company: dict[int, list[Contact]] = {}
    seen_contact_ids: set[int] = set()
    generated_contact_ids: set[int] = set()
    for contact_id in contact_ids:
        if contact_id in seen_contact_ids:
            continue
        seen_contact_ids.add(contact_id)
        contact = session.get(Contact, contact_id)
        if contact is None or contact.status != "active":
            stats["skipped"] += 1
            continue
        selected_contacts_by_company.setdefault(contact.company_id, []).append(contact)

    for company_id, company_contacts in selected_contacts_by_company.items():
        contact = _preferred_contact_for_company(company_contacts)
        stats["processed"] += 1
        company = session.get(Company, company_id)
        if company is None or company.status in {"replied", "unsubscribed", "paused", "blacklisted"}:
            stats["skipped"] += 1
            continue
        cc_emails = _company_cc_emails(session, company.id, contact)
        existing = session.exec(
            select(EmailDraft).where(
                EmailDraft.company_id == company.id,
                EmailDraft.status.in_(["pending_review", "approved", "queued"]),
                EmailDraft.follow_up_step == 0,
            )
        ).first()
        if existing:
            original_status = existing.status
            refreshed_existing = False
            try:
                existing.contact_id = contact.id
                refresh_draft_from_template(session, existing, template)
                existing.cc_emails = cc_emails or (template.cc_emails if template.cc_enabled else None)
                issues = audit_draft(session, existing)
                refreshed_existing = True
            except Exception as exc:
                issues = [f"Template render error: {exc}"]
            if issues:
                existing.status = "pending_review"
                existing.sender_account_id = None
                existing.scheduled_at = None
                existing.error_message = "; ".join(issues)
                existing.approved_at = None
                stats["exceptions"] += 1
            elif original_status == "queued":
                existing.status = "queued"
                existing.error_message = None
                stats["auto_approved"] += 1
            else:
                existing.status = "approved"
                existing.error_message = None
                existing.approved_at = utc_now()
                stats["auto_approved"] += 1
            existing.updated_at = utc_now()
            session.add(existing)
            stats["updated"] += 1
            if refreshed_existing:
                generated_contact_ids.add(contact.id)
            continue
        signature_template = get_signature_template(session)
        try:
            subject, html, text, snapshot = render_template(template, company, contact, session=session)
            render_error = None
        except Exception as exc:
            subject = template.subject
            html = template.body_html
            text = template.body_text
            snapshot = f'{{"template_id": {template.id}, "render_error": {json.dumps(str(exc), ensure_ascii=False)}}}'
            render_error = f"Template render error: {exc}"
        draft = EmailDraft(
            company_id=company.id,
            contact_id=contact.id,
            template_id=template.id,
            signature_template_id=signature_template.id if signature_template else None,
            subject=subject,
            body_html=html,
            body_text=text,
            cc_emails=cc_emails or (template.cc_emails if template.cc_enabled else None),
            template_snapshot=snapshot,
        )
        session.add(draft)
        session.flush()
        if render_error:
            draft.status = "pending_review"
            draft.error_message = render_error
            draft.updated_at = utc_now()
            session.add(draft)
            stats["exceptions"] += 1
        else:
            apply_audit_result(session, draft)
            if draft.status == "pending_review":
                stats["exceptions"] += 1
            else:
                stats["auto_approved"] += 1
        stats["created"] += 1
        generated_contact_ids.add(contact.id)
    if generated_contact_ids:
        result = session.exec(delete(DraftPrepItem).where(DraftPrepItem.contact_id.in_(generated_contact_ids)))
        stats["consumed_prep"] = result.rowcount or 0
    session.commit()
    return stats


def _merge_generate_stats(base: dict[str, int], update: dict[str, int]) -> dict[str, int]:
    for key, value in update.items():
        base[key] = base.get(key, 0) + value
    return base


def _parse_filter_date(value: str, end_of_day: bool = False) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    local_zone = ZoneInfo(get_settings().app_timezone)
    if end_of_day:
        parsed = parsed.replace(hour=23, minute=59, second=59)
    return parsed.replace(tzinfo=local_zone).astimezone(timezone.utc)


def _crm_candidate_contacts(
    session: Session,
    companies: list[Company],
    contacts: list[Contact],
    followup_filter: str,
    last_before: str,
    last_after: str,
    country: str,
    priority: str,
) -> list[Contact]:
    company_by_id = {company.id: company for company in companies}
    suppressions = session.exec(select(Suppression)).all()
    before_dt = _parse_filter_date(last_before, end_of_day=True)
    after_dt = _parse_filter_date(last_after)
    country_value = country.strip().lower()
    priority_value = priority.strip().lower()
    candidates: list[Contact] = []
    for contact in contacts:
        company = company_by_id.get(contact.company_id)
        if contact.status != "active" or company is None:
            continue
        contact_email = (contact.email or "").strip().lower()
        if not contact_email:
            continue
        if any(suppression_matches_email(item, contact_email) for item in suppressions):
            continue
        if company.status in {"unsubscribed", "paused", "blacklisted"}:
            continue
        if country_value and (company.country or "").strip().lower() != country_value:
            continue
        if priority_value and company.priority.strip().lower() != priority_value:
            continue
        last_contact_at = company.last_contact_at
        if last_contact_at is not None and last_contact_at.tzinfo is None:
            last_contact_at = last_contact_at.replace(tzinfo=timezone.utc)
        if followup_filter == "never" and last_contact_at is not None:
            continue
        if followup_filter == "contacted" and last_contact_at is None:
            continue
        if before_dt and (last_contact_at is None or last_contact_at > before_dt):
            continue
        if after_dt and (last_contact_at is None or last_contact_at < after_dt):
            continue
        candidates.append(contact)
    return candidates


def _routes_by_contact(session: Session, contact_ids: list[int]) -> dict[int, list[ContactRoute]]:
    if not contact_ids:
        return {}
    routes = session.exec(
        select(ContactRoute)
        .where(ContactRoute.contact_id.in_(contact_ids))
        .order_by(ContactRoute.route_type, ContactRoute.route_value)
    ).all()
    grouped: dict[int, list[ContactRoute]] = {}
    for route in routes:
        grouped.setdefault(route.contact_id, []).append(route)
    return grouped


def _route_sort_key(route: ContactRoute) -> tuple[int, str]:
    priority = {"email": 0, "linkedin": 1, "whatsapp": 2, "phone": 3}
    if route.is_primary:
        return (-1, route.route_value.lower())
    return (priority.get(route.route_type, 9), route.route_value.lower())


def _primary_route(routes: list[ContactRoute], contact: Contact) -> ContactRoute | None:
    if routes:
        return sorted(routes, key=_route_sort_key)[0]
    if contact.email:
        return ContactRoute(
            contact_id=contact.id or 0,
            route_type="email",
            route_value=contact.email,
            source="contact",
            raw_cell=contact.email,
            is_primary=True,
        )
    return None


def _crm_company_rows(
    companies: list[Company],
    contacts: list[Contact],
    routes_by_contact: dict[int, list[ContactRoute]],
) -> list[dict]:
    contacts_by_company: dict[int, list[Contact]] = {}
    for contact in contacts:
        contacts_by_company.setdefault(contact.company_id, []).append(contact)

    rows: list[dict] = []
    for company in companies:
        company_contacts = sorted(
            contacts_by_company.get(company.id or 0, []),
            key=lambda item: (not item.is_primary, (item.full_name or "").lower(), item.id or 0),
        )
        contact_rows = []
        route_counts: dict[str, int] = {}
        for contact in company_contacts:
            routes = sorted(routes_by_contact.get(contact.id, []), key=_route_sort_key)
            primary_route = _primary_route(routes, contact)
            for route in routes:
                route_counts[route.route_type] = route_counts.get(route.route_type, 0) + 1
            contact_rows.append(
                {
                    "contact": contact,
                    "routes": routes,
                    "primary_route": primary_route,
                    "route_count": len(routes) or int(primary_route is not None),
                }
            )
        rows.append(
            {
                "company": company,
                "contacts": contact_rows,
                "contact_count": len(contact_rows),
                "route_count": sum(route_counts.values()),
                "route_counts": route_counts,
            }
        )
    return rows


def _sync_primary_email_route(session: Session, contact: Contact, source: str = "manual") -> None:
    if contact.id is None or not contact.email:
        return
    route = session.exec(
        select(ContactRoute).where(
            ContactRoute.contact_id == contact.id,
            ContactRoute.route_type == "email",
            ContactRoute.route_value == contact.email,
        )
    ).first()
    if route is None:
        route = ContactRoute(
            contact_id=contact.id,
            route_type="email",
            route_value=contact.email,
            source=source,
            raw_cell=contact.email,
            is_primary=True,
            status="active",
        )
    else:
        route.contact_id = contact.id
        route.is_primary = True
        route.updated_at = utc_now()
    session.add(route)


def _duplicate_company_batch_names(session: Session, drafts: list[EmailDraft], limit: int = 8) -> list[str]:
    labels: list[str] = []
    counts: dict[int, int] = {}
    domain_companies: dict[str, list[str]] = {}
    for draft in drafts:
        counts[draft.company_id] = counts.get(draft.company_id, 0) + 1
        contact = session.get(Contact, draft.contact_id)
        domain = recipient_company_domain(contact.email if contact else None)
        if domain:
            company = session.get(Company, draft.company_id)
            domain_companies.setdefault(domain, []).append(company.name if company else f"Company #{draft.company_id}")
    for company_id, count in counts.items():
        if count > 1:
            company = session.get(Company, company_id)
            labels.append(company.name if company else f"Company #{company_id}")
    for domain, names in domain_companies.items():
        if len(names) > 1:
            labels.append(f"{domain} ({', '.join(dict.fromkeys(names))})")
    if len(labels) > limit:
        return labels[:limit] + [f"+{len(labels) - limit} more"]
    return labels


def _dedupe_batch_drafts(session: Session, drafts: list[EmailDraft]) -> tuple[list[EmailDraft], list[str]]:
    """Keep the first confirmed draft for each company and enterprise email domain."""
    kept: list[EmailDraft] = []
    exclusions: list[str] = []
    seen_company_ids: set[int] = set()
    seen_domains: set[str] = set()
    for draft in drafts:
        company = session.get(Company, draft.company_id)
        company_name = company.name if company else f"Company #{draft.company_id}"
        contact = session.get(Contact, draft.contact_id)
        domain = recipient_company_domain(contact.email if contact else None)
        reason = None
        if draft.company_id in seen_company_ids:
            reason = "duplicate company"
        elif domain and domain in seen_domains:
            reason = f"duplicate enterprise domain {domain}"
        if reason:
            draft.status = "pending_review"
            draft.sender_account_id = None
            draft.scheduled_at = None
            draft.approved_at = None
            draft.error_message = f"Excluded from this batch: {reason}."
            draft.updated_at = utc_now()
            session.add(draft)
            exclusions.append(f"Draft #{draft.id}: {reason} for {company_name}")
            continue
        kept.append(draft)
        seen_company_ids.add(draft.company_id)
        if domain:
            seen_domains.add(domain)
    return kept, exclusions


@app.get("/")
def index(
    request: Request,
    message: str = "",
    queue_sort: str = "status",
    queue_dir: str = "asc",
    queue_view: str = "queued",
    crm_followup: str = "never",
    crm_last_before: str = "",
    crm_last_after: str = "",
    crm_country: str = "",
    crm_priority: str = "",
    crm_page: int = 1,
    session: Session = Depends(get_session),
):
    refresh_pending_audits(session)
    companies = session.exec(select(Company).order_by(Company.updated_at.desc())).all()
    # The unified database retains all raw BD contact rows, including rows that
    # have no sendable email. The dashboard only needs email-bearing contacts;
    # filtering them in SQL avoids rendering thousands of non-actionable rows.
    contacts = session.exec(
        select(Contact)
        .where(Contact.email.is_not(None), Contact.email != "")
        .order_by(Contact.created_at.desc())
    ).all()
    routes_by_contact = _routes_by_contact(session, [contact.id for contact in contacts if contact.id is not None])
    approved_drafts = session.exec(
        select(EmailDraft).where(EmailDraft.status == "approved").order_by(EmailDraft.approved_at.desc()).limit(250)
    ).all()
    exception_drafts = session.exec(
        select(EmailDraft)
        .where(EmailDraft.status == "pending_review")
        .order_by(EmailDraft.updated_at.desc())
        .limit(10)
    ).all()
    queue_drafts = session.exec(
        select(EmailDraft).where(EmailDraft.status == "queued").order_by(EmailDraft.scheduled_at).limit(250)
    ).all()
    senders = session.exec(
        select(SenderAccount)
        .where(SenderAccount.email != PLACEHOLDER_SENDER_EMAIL)
        .order_by(SenderAccount.email)
    ).all()
    email_templates = session.exec(
        select(EmailTemplate).where(mail_template_filter()).order_by(EmailTemplate.created_at.desc())
    ).all()
    active_templates = [template for template in email_templates if template.is_active]

    company_by_id = {company.id: company for company in companies}
    contact_by_id = {contact.id: contact for contact in contacts}
    sender_by_id = {sender.id: sender for sender in senders}
    template_by_id = {template.id: template for template in email_templates}
    # Queue intake creates the immutable rendered snapshot once. Re-rendering
    # every queued draft just to paint this page was both misleading and a
    # large N+1 cost on a populated queue.
    queue_previews = {
        draft.id: {"subject": draft.subject, "body_html": draft.body_html}
        for draft in queue_drafts
    }
    queue_drafts = _sort_queue_drafts(queue_drafts, company_by_id, contact_by_id, queue_sort, queue_dir)
    queue_state = reconcile_queue_state(session)
    queue_paused = queue_state["is_paused"]
    send_records = session.exec(select(SendRecord).order_by(SendRecord.sent_at.desc()).limit(200)).all()
    send_records = _sort_send_records(send_records, company_by_id, contact_by_id, queue_sort, queue_dir)
    sent_count = session.exec(select(func.count(SendRecord.id))).one()
    prep_items = session.exec(select(DraftPrepItem).order_by(DraftPrepItem.created_at.desc())).all()
    prep_contact_ids = [item.contact_id for item in prep_items]
    draftable_contacts = [
        contact
        for contact in contacts
        if contact.id in prep_contact_ids
        and contact.status == "active"
        and company_by_id.get(contact.company_id) is not None
    ]
    all_crm_candidate_contacts = _crm_candidate_contacts(
        session,
        companies,
        contacts,
        crm_followup,
        crm_last_before,
        crm_last_after,
        crm_country,
        crm_priority,
    )
    crm_candidate_total = len(all_crm_candidate_contacts)
    crm_page_count = max(1, (crm_candidate_total + CRM_CANDIDATE_PAGE_SIZE - 1) // CRM_CANDIDATE_PAGE_SIZE)
    crm_page = min(max(1, crm_page), crm_page_count)
    crm_page_start = (crm_page - 1) * CRM_CANDIDATE_PAGE_SIZE
    crm_candidate_contacts = all_crm_candidate_contacts[crm_page_start : crm_page_start + CRM_CANDIDATE_PAGE_SIZE]
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "settings": get_settings(),
            "app_version": APP_VERSION,
            "message": message,
            "companies": companies,
            "contacts": contacts,
            "routes_by_contact": routes_by_contact,
            "draftable_contacts": draftable_contacts,
            "drafts": approved_drafts + exception_drafts + queue_drafts,
            "approved_drafts": approved_drafts,
            "exception_drafts": exception_drafts,
            "queue_drafts": queue_drafts,
            "queue_previews": queue_previews,
            "queue_paused": queue_paused,
            "has_queued_drafts": queue_state["has_queue"],
            "queue_state": queue_state,
            "crm_candidate_contacts": crm_candidate_contacts,
            "crm_candidate_total": crm_candidate_total,
            "crm_page": crm_page,
            "crm_page_count": crm_page_count,
            "crm_filters": {
                "followup": crm_followup,
                "last_before": crm_last_before,
                "last_after": crm_last_after,
                "country": crm_country,
                "priority": crm_priority,
            },
            "send_records": send_records,
            "sent_count": sent_count,
            "queue_sort": queue_sort,
            "queue_dir": queue_dir,
            "queue_view": queue_view,
            "senders": senders,
            "sender_usage": _sender_usage(session),
            "templates": email_templates,
            "active_templates": active_templates,
            "company_by_id": company_by_id,
            "contact_by_id": contact_by_id,
            "stats": _dashboard_stats(session),
            "default_recommended_daily_limit": DEFAULT_RECOMMENDED_DAILY_LIMIT,
            "display_dt": display_dt,
            "server_now": local_now(),
        },
    )


@app.get("/signature/preview")
def signature_preview(
    contact_id: int | None = None,
    sender_id: int | None = None,
    session: Session = Depends(get_session),
):
    config = get_signature_config(session)
    contact = session.get(Contact, contact_id) if contact_id else None
    company = session.get(Company, contact.company_id) if contact else None
    sender = session.get(SenderAccount, sender_id) if sender_id else None
    if sender and sender.email == PLACEHOLDER_SENDER_EMAIL:
        sender = None
    context = signature_context(config, company=company, contact=contact, sender=sender)
    html = render_signature_html(config, context, session=session) if config.get("enabled", True) else ""
    return {
        "enabled": bool(config.get("enabled", True)),
        "html": html,
        "context": context,
        "contact": contact.full_name if contact else "",
        "company": company.name if company else "",
        "country": company.country if company and company.country else "",
        "region": company.region if company and company.region else "",
        "sender": sender.email if sender else "",
    }


@app.get("/senders")
def senders_page(request: Request, message: str = "", session: Session = Depends(get_session)):
    return templates.TemplateResponse(
        "senders.html",
        {
            "request": request,
            "settings": get_settings(),
            "app_version": APP_VERSION,
            "message": message,
            "sender_usage": _sender_usage(session),
            "stats": _dashboard_stats(session),
            "default_recommended_daily_limit": DEFAULT_RECOMMENDED_DAILY_LIMIT,
        },
    )


@app.get("/send-records")
def send_records_page(request: Request, message: str = "", session: Session = Depends(get_session)):
    send_records = session.exec(select(SendRecord).order_by(SendRecord.sent_at.desc()).limit(1000)).all()
    company_ids = {record.company_id for record in send_records}
    contact_ids = {record.contact_id for record in send_records}
    template_ids = {
        template_id
        for record in send_records
        for template_id in (record.template_id, record.signature_template_id)
        if template_id is not None
    }
    companies = session.exec(select(Company).where(Company.id.in_(company_ids))).all() if company_ids else []
    contacts = session.exec(select(Contact).where(Contact.id.in_(contact_ids))).all() if contact_ids else []
    email_templates = (
        session.exec(select(EmailTemplate).where(EmailTemplate.id.in_(template_ids))).all()
        if template_ids
        else []
    )
    return templates.TemplateResponse(
        "send_records.html",
        {
            "request": request,
            "settings": get_settings(),
            "app_version": APP_VERSION,
            "message": message,
            "send_records": send_records,
            "send_record_count": session.exec(select(func.count(SendRecord.id))).one(),
            "company_by_id": {company.id: company for company in companies},
            "contact_by_id": {contact.id: contact for contact in contacts},
            "template_by_id": {template.id: template for template in email_templates},
            "stats": _dashboard_stats(session),
            "display_dt": display_dt,
        },
    )


@app.post("/bounces/scan")
def scan_bounces_now(session: Session = Depends(get_session)):
    result = scan_bounces(session)
    return _redirect_to(
        "/send-records",
        (
            "退信扫描完成："
            f"扫描 {result['scanned_messages']} 封，"
            f"识别退信 {result['detected_bounces']} 封，"
            f"剔除邮箱 {result['suppressed_emails']} 个，"
            f"错误 {result['errors']} 个。日志：{bounce_log_path()}"
        ),
    )


@app.get("/bounces/log")
def download_bounce_log():
    path = bounce_log_path()
    if not path.exists():
        path.write_text("", encoding="utf-8")
    return FileResponse(path, filename="bounce_scan.log", media_type="text/plain")


@app.post("/settings/send-mode")
def update_send_mode(dry_run_email: str = Form("true")):
    if dry_run_email not in {"true", "false"}:
        return _redirect("Invalid send mode.")
    _update_env_value("DRY_RUN_EMAIL", dry_run_email)
    mode = "演练模式" if dry_run_email == "true" else "真实发送模式"
    return _redirect_to("/settings", f"发送模式已切换为：{mode}")


@app.get("/settings")
def settings_page(request: Request, message: str = "", session: Session = Depends(get_session)):
    signature_config = get_signature_config(session)
    signature_preview_context = signature_context(signature_config)
    signature_preview_html = render_signature_html(signature_config, signature_preview_context, session=session)
    return templates.TemplateResponse(
        "settings.html",
        {
            "request": request,
            "settings": get_settings(),
            "app_version": APP_VERSION,
            "message": message,
            "sender_usage": _sender_usage(session),
            "stats": _dashboard_stats(session),
            "default_recommended_daily_limit": DEFAULT_RECOMMENDED_DAILY_LIMIT,
            "queue_paused": is_queue_paused(session),
            "signature": _signature_form(signature_config),
            "signature_preview_html": signature_preview_html,
        },
    )


@app.post("/settings/signature")
def update_signature_settings(
    enabled: bool = Form(False),
    sender_name: str = Form("Your Name"),
    title_prefix: str = Form("Regional Manager"),
    company_name: str = Form("Your Company"),
    default_region: str = Form("UK"),
    default_email: str = Form("sender@example.com"),
    default_phone: str = Form("+1 555 0100"),
    western_phone: str = Form("+1 555 0100"),
    southeast_asia_phone: str = Form("+1 555 0101"),
    address: str = Form(""),
    country_rules_text: str = Form(""),
    sender_rules_text: str = Form(""),
    image_html: str = Form(""),
    session: Session = Depends(get_session),
):
    config = get_signature_config(session)
    country_rules = parse_country_rules_text(country_rules_text) or config.get("country_rules", [])
    sender_rules = parse_sender_rules_text(sender_rules_text)
    config.update(
        {
            "enabled": enabled,
            "sender_name": sender_name.strip() or "Your Name",
            "title_prefix": title_prefix.strip() or "Regional Manager",
            "company_name": company_name.strip() or "Your Company",
            "default_region": default_region.strip() or "UK",
            "default_email": default_email.strip().lower(),
            "default_phone": default_phone.strip(),
            "western_phone": western_phone.strip(),
            "southeast_asia_phone": southeast_asia_phone.strip(),
            "address": address.strip(),
            "country_rules": country_rules,
            "sender_rules": sender_rules,
            "image_html": image_html.strip(),
        }
    )
    save_signature_config(session, config)
    return _redirect_to("/settings", "Signature settings saved. Queued drafts will use the latest signature when refreshed before sending.")


@app.post("/settings/signature/import-docx")
async def import_signature_docx(file: UploadFile, session: Session = Depends(get_session)):
    if not (file.filename or "").lower().endswith(".docx"):
        return _redirect_to("/settings", "Please upload a .docx signature template file.")
    payload = await file.read()
    try:
        body_html = parse_docx_signature_html(payload, include_text=True)
    except ValueError as exc:
        return _redirect_to("/settings", str(exc))
    body_html = _normalize_template_body(body_html)
    config = get_signature_config(session)
    config["enabled"] = True
    config["image_html"] = ""
    upsert_signature_template(session, body_html, _html_to_text(body_html))
    save_signature_config(session, config)
    return _redirect_to("/settings", "Signature DOCX imported into the shared template store. Review the signature block before sending.")


@app.get("/linkedin-connections")
def linkedin_connections_page(request: Request, message: str = "", query: str = "", batch_size: int = 10):
    try:
        candidates = linkedin_candidates(get_settings().linkedin_master_path, query=query, batch_size=batch_size)
        metrics = linkedin_today_metrics(get_settings().linkedin_master_path)
        confirmed = recent_workbench_confirmations(get_settings().linkedin_master_path)
    except LinkedInConnectionError as exc:
        candidates, metrics, confirmed, message = [], {"people": 0, "batches": 0}, [], str(exc)
    return templates.TemplateResponse("linkedin_connections.html", {"request": request, "app_version": APP_VERSION, "message": message, "query": query, "batch_size": batch_size, "candidates": candidates, "metrics": metrics, "confirmed": confirmed})

@app.post("/linkedin-connections/confirm")
def confirm_linkedin_connection_batch(request: Request, source_record_ids: list[str] = Form(default=[]), batch_size: int = Form(10)):
    mode = "single" if len(source_record_ids) == 1 else "batch"
    try:
        result = confirm_linkedin_connections(get_settings().linkedin_master_path, source_record_ids, mode=mode, backup_dir=get_settings().data_dir / "linkedin_backups")
        if "application/json" in request.headers.get("accept", ""): return JSONResponse({"ok": True, "changed": result["changed"], "mode": mode})
        return _redirect_to("/linkedin-connections", f"Confirmed {len(result['changed'])} LinkedIn connection record(s).")
    except LinkedInConnectionError as exc:
        if "application/json" in request.headers.get("accept", ""): return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return _redirect_to(f"/linkedin-connections?batch_size={batch_size}", str(exc))

@app.post("/linkedin-connections/{source_record_id}/restore")
def restore_linkedin_mistag(source_record_id: str, request: Request):
    try:
        restore_mistag(get_settings().linkedin_master_path, source_record_id)
        if "application/json" in request.headers.get("accept", ""): return JSONResponse({"ok": True, "source_record_id": source_record_id})
        return _redirect_to("/linkedin-connections", "LinkedIn mistag restored.")
    except LinkedInConnectionError as exc:
        if "application/json" in request.headers.get("accept", ""): return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
        return _redirect_to("/linkedin-connections", str(exc))


@app.get("/crm")
def crm_page(request: Request, message: str = "", session: Session = Depends(get_session)):
    companies = session.exec(select(Company).order_by(Company.updated_at.desc())).all()
    contacts = session.exec(select(Contact).order_by(Contact.created_at.desc())).all()
    routes_by_contact = _routes_by_contact(session, [contact.id for contact in contacts if contact.id is not None])
    all_company_rows = _crm_company_rows(companies, contacts, routes_by_contact)
    company_rows = [row for row in all_company_rows if not is_no_go_company(row["company"])]
    no_go_company_rows = [row for row in all_company_rows if is_no_go_company(row["company"])]
    return templates.TemplateResponse(
        "crm.html",
        {
            "request": request,
            "settings": get_settings(),
            "app_version": APP_VERSION,
            "message": message,
            "companies": companies,
            "contacts": contacts,
            "company_rows": company_rows,
            "crm_contact_count": sum(row["contact_count"] for row in company_rows),
            "no_go_company_rows": no_go_company_rows,
            "routes_by_contact": routes_by_contact,
            "stats": _dashboard_stats(session),
            "display_dt": display_dt,
        },
    )


@app.post("/crm/no-go/import")
async def import_no_go_company_list(
    file: UploadFile | None = None,
    session: Session = Depends(get_session),
):
    if file is None or not file.filename:
        return _redirect_to("/crm", "请选择 NO-GO 公司文件。")
    payload = await file.read()
    if len(payload) > 5 * 1024 * 1024:
        return _redirect_to("/crm", "NO-GO 文件超过 5 MB，请缩小后重试。")
    try:
        names = parse_no_go_company_file(payload, file.filename)
    except ValueError as exc:
        return _redirect_to("/crm", str(exc))
    source_label = Path(file.filename).name
    try:
        result = import_no_go_companies(session, names, source_label)
    except ValueError as exc:
        return _redirect_to("/crm", str(exc))
    return _redirect_to(
        "/crm",
        "NO-GO 公司导入完成："
        f"输入={result['input']}，新增={result['created']}，拉黑现有={result['updated']}，"
        f"已在名单={result['already_no_go']}，重复={result['duplicates']}。",
    )


@app.get("/rules")
def rules_page(request: Request, message: str = "", session: Session = Depends(get_session)):
    rules = session.exec(select(FollowUpRule).order_by(FollowUpRule.delay_days)).all()
    email_templates = session.exec(
        select(EmailTemplate).where(mail_template_filter()).order_by(EmailTemplate.created_at.desc())
    ).all()
    return templates.TemplateResponse(
        "rules.html",
        {
            "request": request,
            "settings": get_settings(),
            "app_version": APP_VERSION,
            "message": message,
            "rules": rules,
            "cadence": get_followup_cadence(session),
            "templates": email_templates,
            "stats": _dashboard_stats(session),
        },
    )


@app.get("/compliance")
def compliance_page(request: Request, message: str = "", session: Session = Depends(get_session)):
    suppressions = session.exec(select(Suppression).order_by(Suppression.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse(
        "compliance.html",
        {
            "request": request,
            "settings": get_settings(),
            "app_version": APP_VERSION,
            "message": message,
            "suppressions": suppressions,
            "stats": _dashboard_stats(session),
            "display_dt": display_dt,
        },
    )


@app.post("/import")
async def import_file(file: UploadFile, session: Session = Depends(get_session)):
    payload = await file.read()
    try:
        result = import_contacts(session, payload, file.filename or "customers.xlsx")
    except ValueError as exc:
        return _redirect(str(exc))
    prep_count = _set_draft_prep_contacts(session, result["contact_ids"])
    return _redirect(
        f"BD Excel imported. source={result['source_label']}, sheets={result['valid_sheets']}, "
        f"rows_with_email={result['processed_rows']}, companies+{result['created_companies']}/~{result['updated_companies']}, "
        f"contacts+{result['created_contacts']}/~{result['updated_contacts']}, routes+{result['created_routes']}/~{result['updated_routes']}, "
        f"generic_team={result['generic_contacts']}, skipped_no_email={result['skipped_no_email']}, prep={prep_count}"
    )


@app.post("/api/imports/bd-database-json")
async def import_bd_database_json(file: UploadFile, session: Session = Depends(get_session)):
    payload = await file.read()
    try:
        result = import_bd_json_candidates(session, payload, file.filename or "bd-database.json")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    prep_count = _set_draft_prep_contacts(session, result["contact_ids"])
    return {
        "batch_id": result.get("batch_id", ""),
        "source_system": result.get("source_system", ""),
        "selection_rule": result.get("selection_rule", ""),
        "processed": result["processed_rows"],
        "created_companies": result["created_companies"],
        "updated_companies": result["updated_companies"],
        "created_contacts": result["created_contacts"],
        "updated_contacts": result["updated_contacts"],
        "created_routes": result["created_routes"],
        "updated_routes": result["updated_routes"],
        "duplicate_routes": result["duplicates"],
        "draft_prep_items": prep_count,
        "skipped": result.get("skipped", {}),
    }


@app.post("/draft-prep/clear")
def clear_draft_prep(session: Session = Depends(get_session)):
    session.exec(delete(DraftPrepItem))
    session.commit()
    return _redirect("Draft prep list cleared.")


@app.post("/draft-prep/{contact_id}/remove")
def remove_draft_prep_contact(contact_id: int, session: Session = Depends(get_session)):
    session.exec(delete(DraftPrepItem).where(DraftPrepItem.contact_id == contact_id))
    session.commit()
    return _redirect("Removed from draft prep list. CRM record was kept.")


@app.post("/draft-prep/add-from-crm")
def add_draft_prep_from_crm(contact_ids: list[int] = Form(default=[]), session: Session = Depends(get_session)):
    if not contact_ids:
        return _redirect("Please select at least one CRM contact.")
    added = _add_draft_prep_contacts(session, contact_ids)
    return _redirect(f"Added {added} CRM contact(s) to draft prep.")


@app.post("/draft-prep/add")
def add_draft_prep_contact(
    company: str = Form(...),
    full_name: str = Form(...),
    email: str = Form(...),
    position: str = Form(""),
    country: str = Form(""),
    region: str = Form(""),
    priority: str = Form("B"),
    session: Session = Depends(get_session),
):
    company_name = company.strip()
    full_name = full_name.strip()
    email_check = validate_contact_email(email, check_deliverability=False)
    normalized_email = email_check.normalized_email or email.strip().lower()
    if not company_name or not full_name or email_check.is_blocking:
        return _redirect("Company, contact name and valid email are required.")

    contact = session.exec(select(Contact).where(Contact.email == normalized_email)).first()
    if contact is None:
        company_record = session.exec(select(Company).where(Company.name == company_name)).first()
        if company_record is None:
            company_record = Company(
                name=company_name,
                country=country.strip() or None,
                region=region.strip() or None,
                priority=priority.strip() or "B",
            )
            session.add(company_record)
            session.commit()
            session.refresh(company_record)
        else:
            company_record.country = country.strip() or company_record.country
            company_record.region = region.strip() or company_record.region
            company_record.priority = priority.strip() or company_record.priority
        contact = Contact(
            company_id=company_record.id,
            full_name=full_name,
            first_name=full_name.split(" ")[0],
            position=position.strip() or None,
            email=normalized_email,
            is_primary=session.exec(select(Contact).where(Contact.company_id == company_record.id)).first() is None,
            status="active",
        )
        company_record.updated_at = utc_now()
        session.add(contact)
        session.add(company_record)
        session.commit()
        session.refresh(contact)

    _sync_primary_email_route(session, contact)
    session.commit()
    if not session.exec(select(DraftPrepItem).where(DraftPrepItem.contact_id == contact.id)).first():
        session.add(DraftPrepItem(contact_id=contact.id))
        session.commit()
    return _redirect("Customer added to draft prep list.")


@app.post("/draft-prep/{contact_id}/update")
def update_draft_prep_contact(
    contact_id: int,
    company: str = Form(...),
    full_name: str = Form(...),
    email: str = Form(...),
    position: str = Form(""),
    country: str = Form(""),
    region: str = Form(""),
    priority: str = Form("B"),
    session: Session = Depends(get_session),
):
    contact = session.get(Contact, contact_id)
    if contact is None:
        return _redirect("Contact not found.")
    email_check = validate_contact_email(email, check_deliverability=False)
    normalized_email = email_check.normalized_email or email.strip().lower()
    if email_check.is_blocking:
        return _redirect("Please enter a valid email before saving.")
    existing = session.exec(select(Contact).where(Contact.email == normalized_email, Contact.id != contact_id)).first()
    if existing:
        return _redirect("Another CRM contact already uses this email.")
    company_name = company.strip()
    if not company_name or not full_name.strip():
        return _redirect("Company and contact name are required.")
    company_record = session.exec(select(Company).where(Company.name == company_name)).first()
    if company_record is None:
        company_record = Company(name=company_name)
        session.add(company_record)
        session.commit()
        session.refresh(company_record)
    company_record.country = country.strip() or company_record.country
    company_record.region = region.strip() or company_record.region
    company_record.priority = priority.strip() or company_record.priority
    company_record.updated_at = utc_now()
    contact.company_id = company_record.id
    contact.full_name = full_name.strip()
    contact.first_name = contact.full_name.split(" ")[0]
    contact.position = position.strip() or None
    contact.email = normalized_email
    contact.updated_at = utc_now()
    session.add(company_record)
    session.add(contact)
    _sync_primary_email_route(session, contact)
    session.commit()
    return _redirect("Draft prep customer updated.")


@app.get("/templates")
def templates_page(request: Request, message: str = "", session: Session = Depends(get_session)):
    email_templates = session.exec(
        select(EmailTemplate)
        .where(mail_template_filter(), EmailTemplate.is_active == True)
        .order_by(EmailTemplate.created_at.desc())
    ).all()
    signature_template = get_signature_template(session)
    return templates.TemplateResponse(
        "templates.html",
        {
            "request": request,
            "settings": get_settings(),
            "app_version": APP_VERSION,
            "message": message,
            "templates": email_templates,
            "signature_template": signature_template,
            "default_signature_template_name": DEFAULT_SIGNATURE_TEMPLATE_NAME,
            "stats": _dashboard_stats(session),
        },
    )


@app.get("/planner")
def planner_page(request: Request, message: str = "", session: Session = Depends(get_session)):
    first_touch_templates = session.exec(
        select(EmailTemplate)
        .where(EmailTemplate.template_type == "first_touch", EmailTemplate.is_active == True)
        .order_by(EmailTemplate.name)
    ).all()
    policies = {
        policy.template_id: policy
        for policy in session.exec(select(TemplateRotationPolicy)).all()
    }
    return templates.TemplateResponse(
        "planner.html",
        {
            "request": request,
            "settings": get_settings(),
            "app_version": APP_VERSION,
            "message": message,
            "template_rows": [
                {"template": template, "policy": policies.get(template.id)}
                for template in first_touch_templates
            ],
            "stats": _dashboard_stats(session),
        },
    )


@app.post("/planner/policies/{template_id}")
def save_planner_policy(
    template_id: int,
    scope: str = Form("general"),
    match_keywords: str = Form(""),
    priority: int = Form(100),
    is_enabled: bool = Form(False),
    session: Session = Depends(get_session),
):
    try:
        update_rotation_policy(
            session,
            template_id,
            scope=scope,
            match_keywords=match_keywords,
            priority=priority,
            is_enabled=is_enabled,
        )
    except ValueError as exc:
        return _redirect_to("/planner", str(exc))
    session.commit()
    return _redirect_to("/planner", "模板轮换策略已保存。")


@app.post("/api/planner/run")
def run_integrated_planner(limit: int = Form(5000), session: Session = Depends(get_session)):
    """Read-only planning endpoint; it cannot create a draft, queue item, or send."""
    bounded_limit = max(1, min(limit, 5000))
    return JSONResponse(build_batch_plan(session, limit=bounded_limit))


@app.post("/templates")
def create_template(
    name: str = Form(...),
    template_type: str = Form("first_touch"),
    subject: str = Form(...),
    body_html: str = Form(...),
    body_text: str = Form(""),
    cc_enabled: bool = Form(False),
    cc_emails: str = Form(""),
    session: Session = Depends(get_session),
):
    body_html = _normalize_template_body(body_html)
    body_text = body_text or _html_to_text(body_html)
    try:
        _create_mail_template_version(
            session,
            name=name,
            template_type=template_type,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            cc_enabled=cc_enabled,
            cc_emails=cc_emails,
        )
    except TemplateFieldResolutionError as exc:
        return _redirect_to("/templates", str(exc))
    session.commit()
    return _redirect_to("/templates", "Template version saved with a new immutable ID.")


@app.post("/templates/signature")
def save_signature_template(
    name: str = Form(DEFAULT_SIGNATURE_TEMPLATE_NAME),
    body_html: str = Form(...),
    body_text: str = Form(""),
    session: Session = Depends(get_session),
):
    body_html = _normalize_template_body(body_html)
    if not body_html:
        return _redirect_to("/templates", "Signature body is empty.")
    body_text = body_text or _html_to_text(body_html)
    config = get_signature_config(session)
    config["enabled"] = True
    config["image_html"] = ""
    upsert_signature_template(session, body_html, body_text, name=name)
    save_signature_config(session, config)
    return _redirect_to("/templates", "Signature template saved.")


@app.post("/templates/signature/import-docx")
async def import_signature_template_file(
    file: UploadFile,
    name: str = Form(DEFAULT_SIGNATURE_TEMPLATE_NAME),
    session: Session = Depends(get_session),
):
    if not (file.filename or "").lower().endswith(".docx"):
        return _redirect_to("/templates", "Please upload a .docx signature template file.")
    payload = await file.read()
    try:
        body_html = parse_docx_signature_html(payload, include_text=True)
    except ValueError as exc:
        return _redirect_to("/templates", str(exc))
    body_html = _normalize_template_body(body_html)
    body_text = _html_to_text(body_html)
    config = get_signature_config(session)
    config["enabled"] = True
    config["image_html"] = ""
    upsert_signature_template(session, body_html, body_text, name=name)
    save_signature_config(session, config)
    return _redirect_to("/templates", "Signature DOCX imported into the shared template store.")


@app.post("/templates/{template_id}/settings")
def update_template(
    template_id: int,
    name: str = Form(...),
    template_type: str = Form("first_touch"),
    subject: str = Form(...),
    body_html: str = Form(...),
    body_text: str = Form(""),
    cc_enabled: bool = Form(False),
    cc_emails: str = Form(""),
    is_active: bool = Form(False),
    session: Session = Depends(get_session),
):
    template = session.get(EmailTemplate, template_id)
    if template is None:
        return _redirect_to("/templates", "Template not found.")
    body_html = _normalize_template_body(body_html)
    body_text = body_text or _html_to_text(body_html)
    try:
        new_template = _create_mail_template_version(
            session,
            name=name,
            template_type=template_type,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            cc_enabled=cc_enabled,
            cc_emails=cc_emails,
            is_active=is_active,
            replaces_template_id=template.id,
        )
    except TemplateFieldResolutionError as exc:
        return _redirect_to("/templates", str(exc))
    session.commit()
    return _redirect_to(
        "/templates",
        f"Template version saved. Previous ID={template.id}; new ID={new_template.id}.",
    )


@app.post("/templates/{template_id}/delete")
def delete_template(template_id: int, session: Session = Depends(get_session)):
    template = session.get(EmailTemplate, template_id)
    if template is None:
        return _redirect_to("/templates", "Template not found.")

    draft_count = session.exec(select(func.count(EmailDraft.id)).where(EmailDraft.template_id == template_id)).one()
    send_count = session.exec(
        select(func.count(SendRecord.id)).where(
            (SendRecord.template_id == template_id)
            | (SendRecord.signature_template_id == template_id)
        )
    ).one()
    rule_count = session.exec(select(func.count(FollowUpRule.id)).where(FollowUpRule.template_id == template_id)).one()
    if draft_count or send_count or rule_count:
        template.is_active = False
        template.updated_at = utc_now()
        session.add(template)
        session.commit()
        return _redirect_to(
            "/templates",
            f"Template is used by {draft_count} draft(s), {send_count} send record(s), and "
            f"{rule_count} follow-up rule(s), so it was deactivated instead of deleted.",
        )

    template_name = template.name
    session.delete(template)
    session.commit()
    return _redirect_to("/templates", f"Template deleted: {template_name}")


@app.post("/templates/import")
async def import_template_file(
    file: UploadFile,
    name: str = Form(...),
    template_type: str = Form("first_touch"),
    subject: str = Form(...),
    body_text: str = Form(""),
    session: Session = Depends(get_session),
):
    payload = await file.read()
    try:
        body_html = payload.decode("utf-8-sig")
    except UnicodeDecodeError:
        return _redirect_to("/templates", "Template file must be UTF-8 encoded.")
    if not body_html.strip():
        return _redirect_to("/templates", "Template file is empty.")
    body_html = _normalize_template_body(body_html)
    body_text = body_text or _html_to_text(body_html)
    try:
        _create_mail_template_version(
            session,
            name=name,
            template_type=template_type,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            cc_enabled=False,
            cc_emails=None,
        )
    except TemplateFieldResolutionError as exc:
        return _redirect_to("/templates", str(exc))
    session.commit()
    return _redirect_to("/templates", "Template imported as a new immutable version.")


@app.post("/templates/import-docx")
async def import_docx_template_file(
    file: UploadFile,
    name: str = Form(""),
    template_type: str = Form("first_touch"),
    subject: str = Form(""),
    session: Session = Depends(get_session),
):
    if not (file.filename or "").lower().endswith(".docx"):
        return _redirect_to("/templates", "Please upload a .docx template file.")
    template_name = name.strip() or _template_name_from_upload(file.filename)
    if not template_name:
        return _redirect_to("/templates", "Template name is empty.")
    payload = await file.read()
    try:
        subject, body_html, body_text = parse_docx_email_template(payload, fallback_subject=subject)
    except ValueError as exc:
        return _redirect_to("/templates", str(exc))
    try:
        _create_mail_template_version(
            session,
            name=template_name,
            template_type=template_type,
            subject=subject,
            body_html=body_html,
            body_text=body_text,
            cc_enabled=False,
            cc_emails=None,
        )
    except TemplateFieldResolutionError as exc:
        return _redirect_to("/templates", str(exc))
    session.commit()
    return _redirect_to("/templates", f"DOCX template imported as a new immutable version. Subject: {subject}")


@app.post("/senders")
def create_sender(
    name: str = Form(...),
    email: str = Form(...),
    smtp_host: str = Form(...),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    password_env: str = Form(""),
    daily_limit: int = Form(30),
    window_start: str = Form("09:30"),
    window_end: str = Form("17:30"),
    send_timezone: str = Form("Europe/London"),
    send_windows: str = Form("09:30-11:30, 14:00-16:30"),
    interval_mode: str = Form("random"),
    random_delay_min_seconds: str = Form("180"),
    random_delay_max_seconds: str = Form("600"),
    fixed_delay_seconds: str = Form("300"),
    enable_open_tracking: bool = Form(False),
    session: Session = Depends(get_session),
):
    try:
        parsed_window_start, parsed_window_end = _parse_sender_window(window_start, window_end)
        parsed_send_timezone = validate_sender_timezone(send_timezone)
        parsed_send_windows = parse_sender_windows_text(
            send_windows,
            fallback_start=parsed_window_start,
            fallback_end=parsed_window_end,
        )
        delay_min, delay_max = _parse_delay_settings(
            interval_mode,
            random_delay_min_seconds,
            random_delay_max_seconds,
            fixed_delay_seconds,
        )
    except ValueError as exc:
        return _redirect_to("/settings", str(exc))
    sender = SenderAccount(
        name=name,
        email=email.strip().lower(),
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_username=smtp_username or email.strip().lower(),
        password_env=password_env or None,
        smtp_password_encrypted=encrypt_secret(smtp_password) if smtp_password else None,
        daily_limit=daily_limit,
        window_start=parsed_window_start,
        window_end=parsed_window_end,
        send_timezone=parsed_send_timezone,
        send_windows_json=parsed_send_windows,
        random_delay_min_seconds=delay_min,
        random_delay_max_seconds=delay_max,
        enable_open_tracking=enable_open_tracking,
    )
    session.add(sender)
    session.commit()
    return _redirect_to("/settings", "Sender account saved.")


@app.post("/senders/{sender_id}/daily-limit")
def update_sender_daily_limit(
    sender_id: int,
    daily_limit: int = Form(...),
    session: Session = Depends(get_session),
):
    sender = session.get(SenderAccount, sender_id)
    if sender is None:
        return _redirect_to("/settings", "Sender account not found.")
    if daily_limit < 1:
        return _redirect_to("/settings", "Daily max must be at least 1.")
    if daily_limit > 10000:
        return _redirect_to("/settings", "Daily max is too high for this local BD tool.")
    sender.daily_limit = daily_limit
    sender.updated_at = utc_now()
    session.add(sender)
    session.commit()
    advice = get_sending_limit_advice(sender)
    return _redirect_to("/settings", f"Daily max updated to {daily_limit}. Suggested BD volume for this mailbox: {advice.recommended_daily_limit}/day.")


@app.post("/senders/{sender_id}/window")
def update_sender_window(
    sender_id: int,
    window_start: str = Form(...),
    window_end: str = Form(...),
    session: Session = Depends(get_session),
):
    sender = session.get(SenderAccount, sender_id)
    if sender is None:
        return _redirect_to("/settings", "Sender account not found.")
    try:
        parsed_window_start, parsed_window_end = _parse_sender_window(window_start, window_end)
    except ValueError as exc:
        return _redirect_to("/settings", str(exc))
    sender.window_start = parsed_window_start
    sender.window_end = parsed_window_end
    sender.send_windows_json = parse_sender_windows_text(
        f"{parsed_window_start.strftime('%H:%M')}-{parsed_window_end.strftime('%H:%M')}",
        fallback_start=parsed_window_start,
        fallback_end=parsed_window_end,
    )
    sender.updated_at = utc_now()
    session.add(sender)
    session.flush()
    rescheduled = _reschedule_sender_queue(session, sender)
    session.commit()
    session.refresh(sender)
    if sender.window_start != parsed_window_start or sender.window_end != parsed_window_end:
        return _redirect_to("/settings", "Sender window save failed. Please retry after restarting the app.")
    return _redirect_to(
        "/settings",
        f"Sender window updated to {format_sender_windows(sender)}. Rescheduled queued drafts: {rescheduled}.",
    )


@app.post("/senders/{sender_id}/settings")
def update_sender_settings(
    sender_id: int,
    name: str = Form(...),
    email: str = Form(...),
    smtp_host: str = Form(...),
    smtp_port: int = Form(587),
    smtp_username: str = Form(""),
    smtp_password: str = Form(""),
    password_env: str = Form(""),
    daily_limit: int = Form(30),
    window_start: str = Form("09:30"),
    window_end: str = Form("17:30"),
    send_timezone: str = Form("Europe/London"),
    send_windows: str = Form(""),
    interval_mode: str = Form("random"),
    random_delay_min_seconds: str = Form("180"),
    random_delay_max_seconds: str = Form("600"),
    fixed_delay_seconds: str = Form("300"),
    enable_open_tracking: bool = Form(False),
    is_active: bool = Form(False),
    session: Session = Depends(get_session),
):
    sender = session.get(SenderAccount, sender_id)
    if sender is None:
        return _redirect_to("/settings", "Sender account not found.")
    if daily_limit < 1:
        return _redirect_to("/settings", "Daily max must be at least 1.")
    try:
        parsed_window_start, parsed_window_end = _parse_sender_window(window_start, window_end)
        parsed_send_timezone = validate_sender_timezone(send_timezone)
        parsed_send_windows = parse_sender_windows_text(
            send_windows,
            fallback_start=parsed_window_start,
            fallback_end=parsed_window_end,
        )
        delay_min, delay_max = _parse_delay_settings(
            interval_mode,
            random_delay_min_seconds,
            random_delay_max_seconds,
            fixed_delay_seconds,
        )
    except ValueError as exc:
        return _redirect_to("/settings", str(exc))

    sender.name = name
    sender.email = email.strip().lower()
    sender.smtp_host = smtp_host
    sender.smtp_port = smtp_port
    sender.smtp_username = smtp_username or email.strip().lower()
    if smtp_password:
        sender.smtp_password_encrypted = encrypt_secret(smtp_password)
    sender.password_env = password_env or None
    sender.daily_limit = daily_limit
    sender.window_start = parsed_window_start
    sender.window_end = parsed_window_end
    sender.send_timezone = parsed_send_timezone
    sender.send_windows_json = parsed_send_windows
    sender.random_delay_min_seconds = delay_min
    sender.random_delay_max_seconds = delay_max
    sender.enable_open_tracking = enable_open_tracking
    sender.is_active = is_active
    sender.updated_at = utc_now()
    session.add(sender)
    session.flush()
    rescheduled = _reschedule_sender_queue(session, sender)
    session.commit()
    session.refresh(sender)
    if sender.send_timezone != parsed_send_timezone or format_sender_windows(sender) != format_sender_windows(
        SenderAccount(
            name="verification",
            email="verification@example.com",
            smtp_host="localhost",
            send_windows_json=parsed_send_windows,
        )
    ):
        return _redirect_to("/settings", "Sender settings saved, but send-window verification failed. Please retry after restarting the app.")
    return _redirect_to(
        "/settings",
        f"Sender settings updated. Timezone: {sender.send_timezone}; windows: {format_sender_windows(sender)}. Rescheduled queued drafts: {rescheduled}.",
    )


@app.post("/drafts/generate")
def generate_drafts(
    template_id: int = Form(...),
    contact_ids: list[int] = Form(default=[]),
    session: Session = Depends(get_session),
):
    template = session.get(EmailTemplate, template_id)
    if template is None:
        return _redirect("Template not found.")
    if is_signature_template(template):
        return _redirect("Signature templates cannot be used to generate email drafts.")

    if not contact_ids:
        return _redirect("Please select at least one contact before generating drafts.")

    stats = {"processed": 0, "created": 0, "updated": 0, "auto_approved": 0, "exceptions": 0, "skipped": 0}
    for index in range(0, len(contact_ids), DRAFT_GENERATE_BATCH_SIZE):
        batch = contact_ids[index : index + DRAFT_GENERATE_BATCH_SIZE]
        _merge_generate_stats(stats, _generate_drafts_for_contacts(session, template, batch))
    return _redirect(
        f"Generated/updated {stats['created'] + stats['updated']} draft(s) in batches. "
        f"Auto-approved {stats['auto_approved']}, exceptions {stats['exceptions']}, skipped {stats['skipped']}."
    )


@app.post("/drafts/generate-batch")
def generate_drafts_batch(
    template_id: int = Form(...),
    contact_ids: list[int] = Form(default=[]),
    session: Session = Depends(get_session),
):
    template = session.get(EmailTemplate, template_id)
    if template is None:
        return {"ok": False, "message": "Template not found."}
    if is_signature_template(template):
        return {"ok": False, "message": "Signature templates cannot be used to generate email drafts."}
    if not contact_ids:
        return {"ok": False, "message": "Please select at least one contact before generating drafts."}
    batch = contact_ids[:DRAFT_GENERATE_BATCH_SIZE]
    stats = _generate_drafts_for_contacts(session, template, batch)
    return {
        "ok": True,
        "batch_size": DRAFT_GENERATE_BATCH_SIZE,
        "requested": len(batch),
        "stats": stats,
    }


@app.post("/drafts/{draft_id}/approve")
def approve(draft_id: int, session: Session = Depends(get_session)):
    try:
        approve_draft(session, draft_id)
        return _redirect("Draft approved.")
    except ValueError as exc:
        return _redirect(str(exc))


@app.post("/drafts/bulk-approve")
def bulk_approve_drafts(draft_ids: list[int] = Form(default=[]), session: Session = Depends(get_session)):
    if not draft_ids:
        return _redirect("Please select at least one audit exception.")
    approved = 0
    for draft_id in draft_ids:
        try:
            approve_draft(session, draft_id)
            approved += 1
        except ValueError:
            continue
    return _redirect(f"Marked {approved} exception draft(s) as sendable.")


@app.post("/drafts/bulk-delete")
def bulk_delete_drafts(draft_ids: list[int] = Form(default=[]), session: Session = Depends(get_session)):
    if not draft_ids:
        return _redirect("Please select at least one draft to delete.")
    deleted = 0
    for draft_id in draft_ids:
        draft = session.get(EmailDraft, draft_id)
        if draft is None or draft.status in {"queued", "sent"}:
            continue
        session.delete(draft)
        deleted += 1
    session.commit()
    return _redirect(f"Deleted {deleted} draft(s).")


@app.post("/drafts/{draft_id}/attachments")
def update_draft_attachments(
    draft_id: int,
    attachment_paths: str = Form(""),
    session: Session = Depends(get_session),
):
    draft = session.get(EmailDraft, draft_id)
    if draft is None or draft.status in {"sent", "simulated"}:
        return _redirect("Draft not found or already sent.")
    draft.attachment_paths = serialize_attachment_paths(attachment_paths)
    issues = audit_draft(session, draft)
    if issues:
        draft.status = "pending_review"
        draft.error_message = "; ".join(issues)
        draft.approved_at = None
    elif draft.status == "pending_review":
        draft.status = "approved"
        draft.error_message = None
        draft.approved_at = utc_now()
    draft.updated_at = utc_now()
    session.add(draft)
    session.add(
        EmailEvent(
            draft_id=draft.id,
            event_type="attachments_updated",
            metadata_json=draft.attachment_paths,
        )
    )
    session.commit()
    count = len(parse_attachment_paths(draft.attachment_paths))
    return _redirect(f"Draft attachment path(s) saved: {count}.")


@app.post("/drafts/{draft_id}/queue")
def queue(
    draft_id: int,
    sender_id: int = Form(...),
    session: Session = Depends(get_session),
):
    try:
        queue_draft(session, draft_id, sender_id)
        sync_power_awake(session)
        return _redirect("Draft queued.")
    except ValueError as exc:
        return _redirect(str(exc))


@app.post("/send/start")
def start_send_batch(
    sender_id: int | None = Form(None),
    sender_ids: list[int] = Form(default=[]),
    send_template_id: str = Form(""),
    send_mode: str = Form("single"),
    draft_ids: list[int] = Form(default=[]),
    force_recent_send: bool = Form(False),
    session: Session = Depends(get_session),
):
    # Direct Python callers see FastAPI's Form default object; HTTP callers
    # receive a real bool. Only an explicit True is an override.
    force_recent_send = force_recent_send is True
    if not draft_ids:
        return _redirect("Please select at least one approved draft before starting.")
    selected_sender_ids = sender_ids or ([sender_id] if sender_id is not None else [])
    selected_sender_ids = list(dict.fromkeys(selected_sender_ids))
    senders = [session.get(SenderAccount, selected_sender_id) for selected_sender_id in selected_sender_ids]
    senders = [
        sender
        for sender in senders
        if sender is not None and sender.is_active and sender.email != PLACEHOLDER_SENDER_EMAIL
    ]
    if not senders:
        return _redirect("Sender account not found.")
    if send_mode not in {"single", "rotate"}:
        send_mode = "single"
    if send_mode == "single":
        senders = senders[:1]
    local_exclusions: list[str] = []
    selected_drafts = [session.get(EmailDraft, draft_id) for draft_id in draft_ids]
    missing_draft_ids = [draft_id for draft_id, draft in zip(draft_ids, selected_drafts) if draft is None]
    if missing_draft_ids:
        local_exclusions.extend(f"Draft #{draft_id}: missing before handoff" for draft_id in missing_draft_ids)
    selected_drafts = [draft for draft in selected_drafts if draft is not None]
    selected_drafts, duplicate_exclusions = _dedupe_batch_drafts(session, selected_drafts)
    local_exclusions.extend(duplicate_exclusions)
    if not selected_drafts:
        return _redirect("Blocked: no confirmed draft remains after local roster exclusions.")
    selected_template_id = int(send_template_id) if send_template_id.strip() else None
    if selected_template_id is not None:
        send_template = session.get(EmailTemplate, selected_template_id)
        if send_template is None:
            return _redirect("Selected send template not found.")
        if is_signature_template(send_template):
            return _redirect("Signature templates cannot be used as send templates.")
    try:
        handoff = create_queue_handoff(
            session,
            [draft.id for draft in selected_drafts],
            [sender.id for sender in senders],
            selected_template_id,
            send_mode,
            allow_recent_duplicate=force_recent_send,
        )
    except ValueError as exc:
        return _redirect(str(exc))
    sync_power_awake(session)
    exclusion_note = f" Local roster exclusions: {len(local_exclusions)}." if local_exclusions else ""
    return _redirect(
        f"Started sending: {handoff.total_count} draft(s) accepted as one queue intake. "
        f"It will import continuously in batches of {handoff.batch_size} with no further confirmation."
        f"{exclusion_note}"
    )


@app.post("/queue/cancel")
def cancel_queue_items(draft_ids: list[int] = Form(default=[]), session: Session = Depends(get_session)):
    if not draft_ids:
        return _redirect("Please select at least one queued draft to cancel.")
    cancelled = 0
    skipped = 0
    for draft_id in draft_ids:
        draft = session.get(EmailDraft, draft_id)
        if draft is None or draft.status != "queued":
            skipped += 1
            continue
        try:
            cancel_queued_draft(session, draft)
            cancelled += 1
        except ValueError:
            skipped += 1
    session.commit()
    reconcile_queue_state(session)
    sync_power_awake(session)
    return _redirect(f"Cancelled queued draft(s): {cancelled}. Skipped: {skipped}.")


@app.post("/queue/cancel-all")
def cancel_all_queue(session: Session = Depends(get_session)):
    drafts = session.exec(select(EmailDraft).where(EmailDraft.status == "queued")).all()
    for draft in drafts:
        cancel_queued_draft(session, draft)
    session.commit()
    reconcile_queue_state(session)
    sync_power_awake(session)
    return _redirect(f"Cancelled all queued drafts: {len(drafts)}.")


@app.post("/queue/pause")
def pause_queue(session: Session = Depends(get_session)):
    queue_state = reconcile_queue_state(session)
    if not queue_state["has_queue"]:
        return _redirect("当前没有待发送队列。")
    set_app_setting(session, "queue_paused", "true")
    queue_state = reconcile_queue_state(session)
    sync_power_awake(session)
    return _redirect("发送队列已暂停。")


@app.post("/queue/resume")
def resume_queue(session: Session = Depends(get_session)):
    queue_state = reconcile_queue_state(session)
    if not queue_state["has_queue"]:
        return _redirect("当前没有待发送队列，已回到开始发送。")
    set_app_setting(session, "queue_paused", "false")
    result = process_due_queue(session, limit=1)
    reconcile_queue_state(session)
    sync_power_awake(session)
    return _redirect(
        f"发送队列已继续。立即处理：sent={result['sent']}, failed={result['failed']}, skipped={result['skipped']}。"
    )


@app.post("/drafts/{draft_id}/mark-replied")
def mark_replied(draft_id: int, session: Session = Depends(get_session)):
    draft = session.get(EmailDraft, draft_id)
    if draft is None:
        return _redirect("Draft not found.")
    company = session.get(Company, draft.company_id)
    if company:
        company.status = "replied"
        company.last_contact_at = utc_now()
        company.updated_at = utc_now()
        session.add(company)
    session.add(EmailEvent(draft_id=draft.id, event_type="manual_reply"))
    session.commit()
    return _redirect("Company marked as replied. Follow-ups will stop.")


@app.post("/queue/process")
def process_queue_now(session: Session = Depends(get_session)):
    result = process_due_queue(session)
    if result.get("paused"):
        sync_power_awake(session)
        return _redirect("发送队列已暂停，点击继续发送后再处理。")
    reconcile_queue_state(session)
    sync_power_awake(session)
    return _redirect(f"Queue processed: sent={result['sent']}, failed={result['failed']}, skipped={result['skipped']}.")


@app.get("/queue/handoff/status")
def queue_handoff_status(session: Session = Depends(get_session)):
    """Read-only progress for the current or most recent one-click intake."""
    handoff = session.exec(
        select(QueueHandoffRun).order_by(QueueHandoffRun.created_at.desc(), QueueHandoffRun.id.desc())
    ).first()
    if handoff is None:
        return {"status": "idle", "total": 0, "cursor": 0, "queued": 0, "excluded": 0, "last_error": None}
    return {
        "id": handoff.id,
        "status": handoff.status,
        "total": handoff.total_count,
        "cursor": handoff.cursor,
        "queued": handoff.queued_count,
        "excluded": handoff.excluded_count,
        "batch_size": handoff.batch_size,
        "last_error": handoff.last_error,
        "updated_at": handoff.updated_at,
    }


@app.post("/followups/scan")
def scan_followups_now(session: Session = Depends(get_session)):
    result = scan_followups(session)
    return _redirect(f"Follow-up scan created {result['followup_drafts']} pending-review draft(s).")


@app.post("/followups/generate-batch")
def generate_followup_batch(
    template_id: int = Form(...),
    limit: int = Form(250),
    priorities: str = Form("A,B,C"),
    session: Session = Depends(get_session),
):
    try:
        result = generate_dual_counter_followups(
            session,
            template_id=template_id,
            priorities=priorities,
            limit=limit,
        )
    except ValueError as exc:
        return _redirect_to("/rules", str(exc))
    return _redirect_to(
        "/rules",
        f"双计数器手动批次已生成 {result['followup_drafts']} / {limit} 封待审核草稿；未启用自动跟进，未发送邮件。",
    )


@app.post("/followup-rules")
def create_followup_rule(
    name: str = Form(...),
    delay_days: int = Form(...),
    template_id: int = Form(...),
    priorities: str = Form("A,B,C"),
    session: Session = Depends(get_session),
):
    return _redirect_to("/rules", "旧版固定延迟规则已停用；请使用双计数器跟进规则。")


@app.post("/followup-cadence")
def update_followup_cadence(
    enabled: bool = Form(False),
    template_id: int | None = Form(None),
    priorities: str = Form("A,B,C"),
    session: Session = Depends(get_session),
):
    try:
        cadence = save_followup_cadence(
            session,
            enabled=enabled,
            template_id=template_id,
            priorities=priorities,
        )
    except ValueError as exc:
        return _redirect_to("/rules", str(exc))
    state = "已启用" if cadence.enabled else "已保存为停用"
    return _redirect_to(
        "/rules",
        f"双计数器跟进规则{state}：同一联系人每次成功发送后增加 {cadence.contact_step_hours}h；"
        f"更换联系人时，公司级最短间隔 {cadence.company_switch_min_hours}h。",
    )


@app.post("/suppressions")
def add_suppression(email: str = Form(...), reason: str = Form("blacklist"), session: Session = Depends(get_session)):
    normalized = email.strip().lower()
    if not session.exec(select(Suppression).where(Suppression.email == normalized)).first():
        session.add(Suppression(email=normalized, reason=reason))
        session.commit()
    return _redirect("Suppression saved.")


@app.get("/tracking/open/{tracking_id}.png")
def open_tracking(tracking_id: str, request: Request, session: Session = Depends(get_session)):
    draft = session.exec(select(EmailDraft).where(EmailDraft.tracking_id == tracking_id)).first()
    if draft:
        session.add(
            EmailEvent(
                draft_id=draft.id,
                event_type="open",
                metadata_json=json.dumps({"user_agent": request.headers.get("user-agent", "")[:200]}),
            )
        )
        session.commit()
    return Response(content=PIXEL, media_type="image/gif", headers={"Cache-Control": "no-store"})


@app.get("/reports/sending.xlsx")
def report(session: Session = Depends(get_session)):
    path = export_sending_report(session)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Report not found.")
    return FileResponse(path, filename=path.name)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/sync/bd-database/status")
def bd_database_sync_status(session: Session = Depends(get_session)):
    status = get_bd_master_sync_status(session, get_settings().bd_database_path)
    status["scheduler_running"] = scheduler.running
    return status
