import json
import re
from base64 import b64decode
from datetime import datetime, timedelta, timezone
from html import escape
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func
from sqlmodel import Session, select

from app.config import get_settings
from app.db import create_db_and_tables, engine, get_session
from app.models import BounceRecord, Company, Contact, ContactRoute, DraftPrepItem, EmailDraft, EmailEvent, EmailTemplate, FollowUpRule, SendRecord, SenderAccount, Suppression
from app.scheduler import start_scheduler, stop_scheduler
from app.seed import seed_defaults
from app.services.docx_templates import parse_docx_email_template, parse_docx_signature_html
from app.services.importer import import_bd_json_candidates, import_contacts, is_generic_email
from app.services.email_quality import validate_contact_email
from app.services.app_settings import is_queue_paused, set_app_setting
from app.services.queue import (
    audit_draft,
    apply_audit_result,
    approve_draft,
    cancel_queued_draft,
    daily_sent_count,
    next_window_start,
    process_due_queue,
    queue_draft,
    recipient_company_domain,
    recent_company_send_records,
    refresh_draft_from_template,
    refresh_pending_audits,
    scan_followups,
    sender_interval_seconds,
)
from app.services.reports import export_sending_report
from app.services.bounce_scanner import bounce_log_path, scan_bounces
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
from app.services.templates import render_template
from app.time_utils import display_dt, local_now, utc_now
from app.version import APP_VERSION

app = FastAPI(title="BD Email Workbench Lite")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
PIXEL = b64decode("R0lGODlhAQABAPAAAP///wAAACH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==")
DRAFT_GENERATE_BATCH_SIZE = 50
PLACEHOLDER_SENDER_EMAIL = "your.name@example.com"


def attachment_paths_text(value: str | None) -> str:
    return "\n".join(parse_attachment_paths(value))


templates.env.globals["attachment_paths_text"] = attachment_paths_text


def _clear_startup_workspace(session: Session) -> None:
    session.exec(delete(DraftPrepItem))
    session.exec(
        delete(EmailDraft).where(
            EmailDraft.status.in_(["pending_review", "approved", "failed", "skipped", "simulated"]),
            EmailDraft.sent_at == None,
            EmailDraft.scheduled_at == None,
        )
    )


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
        sync_power_awake(session)
    start_scheduler()


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


def _dashboard_stats(session: Session) -> dict[str, int]:
    senders = session.exec(select(SenderAccount).where(SenderAccount.email != PLACEHOLDER_SENDER_EMAIL)).all()
    return {
        "sent_today": sum(daily_sent_count(session, sender.id) for sender in senders),
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
            "sent_today": daily_sent_count(session, sender.id),
            "remaining": max(sender.daily_limit - daily_sent_count(session, sender.id), 0),
            "advice": get_sending_limit_advice(sender),
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
        schedule_at = schedule_at + timedelta(seconds=sender_interval_seconds(sender))
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
    return is_generic_email(contact.email) or "team" in label or "general inbox" in label or "public inbox" in label or " cc" in f" {label}"


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
    return ", ".join(emails) or None


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
    suppressions = {
        suppression.email
        for suppression in session.exec(select(Suppression)).all()
    }
    before_dt = _parse_filter_date(last_before, end_of_day=True)
    after_dt = _parse_filter_date(last_after)
    country_value = country.strip().lower()
    priority_value = priority.strip().lower()
    candidates: list[Contact] = []
    for contact in contacts:
        company = company_by_id.get(contact.company_id)
        if contact.status != "active" or company is None:
            continue
        if contact.email.strip().lower() in suppressions:
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
    session: Session = Depends(get_session),
):
    refresh_pending_audits(session)
    companies = session.exec(select(Company).order_by(Company.updated_at.desc())).all()
    contacts = session.exec(select(Contact).order_by(Contact.created_at.desc())).all()
    routes_by_contact = _routes_by_contact(session, [contact.id for contact in contacts if contact.id is not None])
    approved_drafts = session.exec(
        select(EmailDraft).where(EmailDraft.status == "approved").order_by(EmailDraft.approved_at.desc()).limit(250)
    ).all()
    exception_drafts = session.exec(
        select(EmailDraft)
        .where(EmailDraft.status == "pending_review")
        .order_by(EmailDraft.updated_at.desc())
        .limit(250)
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
    queue_previews: dict[int, dict[str, str]] = {}
    for draft in queue_drafts:
        preview = {"subject": draft.subject, "body_html": draft.body_html}
        company = company_by_id.get(draft.company_id)
        contact = contact_by_id.get(draft.contact_id)
        sender = sender_by_id.get(draft.sender_account_id)
        template = template_by_id.get(draft.template_id)
        if company and contact and sender and template:
            try:
                subject, body_html, _, _ = render_template(template, company, contact, session=session, sender=sender)
                preview = {"subject": subject, "body_html": body_html}
            except Exception:
                pass
        queue_previews[draft.id] = preview
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
    crm_candidate_contacts = _crm_candidate_contacts(
        session,
        companies,
        contacts,
        crm_followup,
        crm_last_before,
        crm_last_after,
        crm_country,
        crm_priority,
    )
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
    companies = session.exec(select(Company).where(Company.id.in_(company_ids))).all() if company_ids else []
    contacts = session.exec(select(Contact).where(Contact.id.in_(contact_ids))).all() if contact_ids else []
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
    company_name: str = Form("Example Company"),
    default_region: str = Form("UK"),
    default_email: str = Form("your.name@example.com"),
    default_phone: str = Form(""),
    western_phone: str = Form(""),
    southeast_asia_phone: str = Form(""),
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
            "company_name": company_name.strip() or "Example Company",
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


@app.get("/crm")
def crm_page(request: Request, message: str = "", session: Session = Depends(get_session)):
    companies = session.exec(select(Company).order_by(Company.updated_at.desc())).all()
    contacts = session.exec(select(Contact).order_by(Contact.created_at.desc())).all()
    routes_by_contact = _routes_by_contact(session, [contact.id for contact in contacts if contact.id is not None])
    company_rows = _crm_company_rows(companies, contacts, routes_by_contact)
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
            "routes_by_contact": routes_by_contact,
            "stats": _dashboard_stats(session),
            "display_dt": display_dt,
        },
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
        select(EmailTemplate).where(mail_template_filter()).order_by(EmailTemplate.created_at.desc())
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
    template = session.exec(select(EmailTemplate).where(EmailTemplate.name == name.strip())).first()
    if template is None:
        template = EmailTemplate(name=name.strip(), template_type=template_type, subject=subject.strip(), body_html=body_html)
    template.name = name.strip()
    template.template_type = template_type
    template.subject = subject.strip()
    template.body_html = body_html
    template.body_text = body_text or None
    template.cc_enabled = cc_enabled
    template.cc_emails = cc_emails.strip() or None
    template.is_active = True
    template.updated_at = utc_now()
    session.add(template)
    session.flush()
    _deactivate_same_name_templates(session, template.id, template.name)
    session.commit()
    return _redirect_to("/templates", "Template saved.")


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
    template.name = name.strip()
    template.template_type = template_type
    template.subject = subject.strip()
    template.body_html = body_html
    template.body_text = body_text or None
    template.cc_enabled = cc_enabled
    template.cc_emails = cc_emails.strip() or None
    template.is_active = is_active
    template.updated_at = utc_now()
    session.add(template)
    session.flush()
    _deactivate_same_name_templates(session, template.id, template.name)
    session.commit()
    return _redirect_to("/templates", "Template updated.")


@app.post("/templates/{template_id}/delete")
def delete_template(template_id: int, session: Session = Depends(get_session)):
    template = session.get(EmailTemplate, template_id)
    if template is None:
        return _redirect_to("/templates", "Template not found.")

    draft_count = session.exec(select(func.count(EmailDraft.id)).where(EmailDraft.template_id == template_id)).one()
    rule_count = session.exec(select(func.count(FollowUpRule.id)).where(FollowUpRule.template_id == template_id)).one()
    if draft_count or rule_count:
        template.is_active = False
        template.updated_at = utc_now()
        session.add(template)
        session.commit()
        return _redirect_to(
            "/templates",
            f"Template is used by {draft_count} draft(s) and {rule_count} follow-up rule(s), so it was deactivated instead of deleted.",
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
    template = session.exec(select(EmailTemplate).where(EmailTemplate.name == name.strip())).first()
    if template is None:
        template = EmailTemplate(name=name.strip(), template_type=template_type, subject=subject.strip(), body_html=body_html)
    template.name = name.strip()
    template.template_type = template_type
    template.subject = subject.strip()
    template.body_html = body_html
    template.body_text = body_text or None
    template.cc_enabled = False
    template.cc_emails = None
    template.is_active = True
    template.updated_at = utc_now()
    session.add(template)
    session.flush()
    _deactivate_same_name_templates(session, template.id, template.name)
    session.commit()
    return _redirect_to("/templates", "Template imported from file.")


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
    template = session.exec(select(EmailTemplate).where(EmailTemplate.name == template_name)).first()
    if template is None:
        template = EmailTemplate(name=template_name, template_type=template_type, subject=subject.strip(), body_html=body_html)
    template.name = template_name
    template.template_type = template_type
    template.subject = subject.strip()
    template.body_html = body_html
    template.body_text = body_text
    template.cc_enabled = False
    template.cc_emails = None
    template.is_active = True
    template.updated_at = utc_now()
    session.add(template)
    session.flush()
    _deactivate_same_name_templates(session, template.id, template.name)
    session.commit()
    return _redirect_to("/templates", f"DOCX template imported. Subject: {subject}")


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
    interval_mode: str = Form("random"),
    random_delay_min_seconds: str = Form("180"),
    random_delay_max_seconds: str = Form("600"),
    fixed_delay_seconds: str = Form("300"),
    enable_open_tracking: bool = Form(False),
    session: Session = Depends(get_session),
):
    try:
        parsed_window_start, parsed_window_end = _parse_sender_window(window_start, window_end)
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
    sender.updated_at = utc_now()
    session.add(sender)
    session.flush()
    rescheduled = _reschedule_sender_queue(session, sender)
    session.commit()
    session.refresh(sender)
    if sender.window_start != parsed_window_start or sender.window_end != parsed_window_end:
        return _redirect_to("/settings", "Sender window save failed. Please retry after restarting the app.")
    overnight = " Overnight window enabled." if parsed_window_start > parsed_window_end else ""
    return _redirect_to(
        "/settings",
        f"Sender window updated to {sender.window_start.strftime('%H:%M')}-{sender.window_end.strftime('%H:%M')}.{overnight} Rescheduled queued drafts: {rescheduled}.",
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
    if sender.window_start != parsed_window_start or sender.window_end != parsed_window_end:
        return _redirect_to("/settings", "Sender settings saved, but window verification failed. Please retry after restarting the app.")
    return _redirect_to(
        "/settings",
        f"Sender settings updated. Window: {sender.window_start.strftime('%H:%M')}-{sender.window_end.strftime('%H:%M')}. Rescheduled queued drafts: {rescheduled}.",
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
    selected_drafts = [session.get(EmailDraft, draft_id) for draft_id in draft_ids]
    selected_drafts = [draft for draft in selected_drafts if draft is not None]
    duplicate_company_names = _duplicate_company_batch_names(session, selected_drafts)
    if duplicate_company_names:
        return _redirect(
            "Blocked: one company can only have one draft in a send batch. "
            f"Duplicate company selection: {', '.join(duplicate_company_names)}."
        )
    selected_template_id = int(send_template_id) if send_template_id.strip() else None
    if selected_template_id is not None:
        send_template = session.get(EmailTemplate, selected_template_id)
        if send_template is None:
            return _redirect("Selected send template not found.")
        if is_signature_template(send_template):
            return _redirect("Signature templates cannot be used as send templates.")
        refresh_errors: list[str] = []
        for draft in selected_drafts:
            try:
                refresh_draft_from_template(session, draft, send_template)
                issues = audit_draft(session, draft)
            except Exception as exc:
                issues = [f"Template render error: {exc}"]
            if issues:
                draft.status = "pending_review"
                draft.sender_account_id = None
                draft.scheduled_at = None
                draft.approved_at = None
                draft.error_message = "; ".join(issues)
                refresh_errors.append(f"Draft #{draft.id}: {draft.error_message}")
            elif draft.status in {"pending_review", "approved"}:
                draft.status = "approved"
                draft.approved_at = utc_now()
                draft.error_message = None
            draft.updated_at = utc_now()
            session.add(draft)
        session.commit()
        if refresh_errors:
            return _redirect(f"Template refresh blocked sending. First issue: {refresh_errors[0]}")
    recent_records = recent_company_send_records(session, {draft.company_id for draft in selected_drafts}, hours=8)
    if recent_records and not force_recent_send:
        company_names = []
        for record in recent_records[:5]:
            company = session.get(Company, record.company_id)
            company_names.append(company.name if company else f"Company #{record.company_id}")
        names = ", ".join(dict.fromkeys(company_names))
        return _redirect(f"8小时内已有发送记录：{names}。请勾选“忽略8小时提醒，仍然继续发送”后再开始。")
    set_app_setting(session, "queue_paused", "false")
    queued = 0
    skipped = 0
    errors: list[str] = []
    queued_draft_ids: list[int] = []
    for index, draft_id in enumerate(draft_ids):
        sender = senders[index % len(senders)]
        try:
            queue_draft(session, draft_id, sender.id, commit=False)
            queued += 1
            queued_draft_ids.append(draft_id)
            if recent_records and force_recent_send:
                session.add(EmailEvent(draft_id=draft_id, event_type="recent_send_override"))
        except ValueError as exc:
            skipped += 1
            errors.append(str(exc))
    session.commit()
    processed_now = {"sent": 0, "failed": 0, "skipped": 0}
    if queued_draft_ids:
        processed_now = process_due_queue(session, limit=1, draft_ids=queued_draft_ids)
    queue_state = reconcile_queue_state(session)
    sync_power_awake(session)
    if errors:
        return _redirect(f"Start send completed with issues: queued={queued}, skipped={skipped}, first issue={errors[0]}")
    return _redirect(
        f"Started sending: mode={send_mode}, sender_count={len(senders)}, queued {queued} draft(s). Current queue={queue_state['queued_count']}. Immediate processing: sent={processed_now['sent']}, failed={processed_now['failed']}, skipped={processed_now['skipped']}. Minimum interval is strictly 30 seconds."
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


@app.post("/followups/scan")
def scan_followups_now(session: Session = Depends(get_session)):
    result = scan_followups(session)
    return _redirect(f"Follow-up scan created {result['followup_drafts']} pending-review draft(s).")


@app.post("/followup-rules")
def create_followup_rule(
    name: str = Form(...),
    delay_days: int = Form(...),
    template_id: int = Form(...),
    priorities: str = Form("A,B,C"),
    session: Session = Depends(get_session),
):
    template = session.get(EmailTemplate, template_id)
    if template is None:
        return _redirect("Follow-up template not found.")
    if is_signature_template(template):
        return _redirect("Signature templates cannot be used for follow-up rules.")
    session.add(FollowUpRule(name=name, delay_days=delay_days, template_id=template_id, priorities=priorities))
    session.commit()
    return _redirect("Follow-up rule saved.")


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
