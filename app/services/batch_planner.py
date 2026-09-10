"""Read-only campaign planning driven by persisted template policies.

This deliberately has no template-ID compatibility list.  A template becomes
eligible because its current policy says so; successful use of that same
template for the company is the only rotation exclusion.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import Company, Contact, EmailDraft, EmailTemplate, SendRecord, Suppression, TemplateRotationPolicy
from app.services.no_go_companies import is_no_go_company
from app.services.email_quality import validate_contact_email
from app.services.queue import resolve_primary_contact
from app.services.render_validation import template_references_field
from app.services.suppression import suppression_matches_email
from app.services.template_targeting import product_match_score, template_matches_target
from app.time_utils import persisted_utc, utc_now

SUCCESS_STATUSES = {"sent", "delivered", "accepted"}
ACTIVE_DRAFT_STATUSES = {"pending_review", "approved", "queued", "sending", "send_unknown"}
SCOPE_GENERAL = "general"
SCOPE_SPORTS_LINE_MARKING = "sports_line_marking"
SCOPE_MANUAL_ONLY = "manual_only"
VALID_SCOPES = {SCOPE_GENERAL, SCOPE_SPORTS_LINE_MARKING, SCOPE_MANUAL_ONLY}
SPECIALIST_DEFAULT_KEYWORDS = "sports,line marking,linemarking,pitch marking,stadium,football,golf,turf"
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
COMPANY_SUCCESS_COOLDOWN_HOURS = 48
RECIPIENT_SUCCESS_COOLDOWN_DAYS = 7
EMAIL_PLANNER_LIBRARIES = {"terminal", "dealer"}


def _normal(value: object) -> str:
    return " ".join(str(value or "").casefold().split())


def _initial_policy(template: EmailTemplate) -> tuple[str, str | None, bool]:
    """One-time policy defaults for known specialist content, by name not ID."""
    if template_references_field(template, "region"):
        return SCOPE_MANUAL_ONLY, None, False
    name = _normal(template.name)
    if "robotics portfolio" in name:
        return SCOPE_MANUAL_ONLY, None, False
    if "paintmaster" in name and ("tour" in name or "dealer" in name):
        return SCOPE_SPORTS_LINE_MARKING, SPECIALIST_DEFAULT_KEYWORDS, True
    return SCOPE_GENERAL, None, True


def ensure_rotation_policy(
    session: Session,
    template: EmailTemplate,
    *,
    replaces_template_id: int | None = None,
) -> TemplateRotationPolicy:
    """Create a policy once; immutable template versions inherit their policy."""
    existing = session.exec(
        select(TemplateRotationPolicy).where(TemplateRotationPolicy.template_id == template.id)
    ).first()
    if existing:
        if template_references_field(template, "region"):
            existing.scope = SCOPE_MANUAL_ONLY
            existing.match_keywords = None
            existing.is_enabled = False
            session.add(existing)
        return existing
    previous = session.exec(
        select(TemplateRotationPolicy).where(TemplateRotationPolicy.template_id == replaces_template_id)
    ).first() if replaces_template_id else None
    if previous:
        policy = TemplateRotationPolicy(
            template_id=template.id,
            scope=previous.scope,
            match_keywords=previous.match_keywords,
            priority=previous.priority,
            is_enabled=previous.is_enabled,
        )
    else:
        scope, keywords, enabled = _initial_policy(template)
        policy = TemplateRotationPolicy(
            template_id=template.id,
            scope=scope,
            match_keywords=keywords,
            priority=template.id or 100,
            is_enabled=enabled,
        )
    if template_references_field(template, "region"):
        policy.scope = SCOPE_MANUAL_ONLY
        policy.match_keywords = None
        policy.is_enabled = False
    session.add(policy)
    return policy


def ensure_rotation_policies(session: Session) -> None:
    """Backfill policies for existing first-touch templates during startup only."""
    templates = session.exec(
        select(EmailTemplate).where(EmailTemplate.template_type == "first_touch")
    ).all()
    for template in templates:
        ensure_rotation_policy(session, template)


def _keyword_list(value: str | None) -> list[str]:
    return [item.strip().casefold() for item in (value or "").split(",") if item.strip()]


def _matches_policy(company: Company, policy: TemplateRotationPolicy) -> bool:
    if not policy.is_enabled or policy.scope == SCOPE_MANUAL_ONLY:
        return False
    if policy.scope == SCOPE_GENERAL:
        return True
    context = " ".join(
        _normal(value)
        for value in (company.name, company.company_type, company.source, company.notes, company.company_intro)
    )
    keywords = _keyword_list(policy.match_keywords) or _keyword_list(SPECIALIST_DEFAULT_KEYWORDS)
    return any(keyword in context for keyword in keywords)


def _company_is_no_go(company: Company) -> bool:
    return is_no_go_company(company)


def _is_suppressed(contact: Contact, suppressions: list[Suppression]) -> bool:
    return any(suppression_matches_email(item, contact.email) for item in suppressions)


def _eligible_primary_contact(session: Session, company_id: str, suppressions: list[Suppression]) -> tuple[Contact | None, str | None]:
    contact = resolve_primary_contact(session, company_id)
    if contact is None:
        return None, "no_active_contact"
    if (contact.email_send_status or "include").casefold() != "include":
        return None, "contact_not_included"
    if _normal(contact.priority_roll_status) == "success_locked":
        return None, "source_success_locked"
    email = (contact.email or "").strip()
    # Planner performs deterministic, zero-network eligibility checks.  The
    # handoff's single recipient audit performs the bounded DNS check before a
    # draft can queue, so previewing thousands of companies stays responsive.
    email_check = validate_contact_email(email, check_deliverability=False)
    if email_check.is_blocking:
        return None, "primary_email_quality_invalid"
    if not EMAIL_RE.match(email):
        return None, "primary_email_invalid"
    if _is_suppressed(contact, suppressions):
        return None, "primary_recipient_suppressed"
    return contact, None


def _latest_successes(session: Session) -> tuple[dict[str, datetime], dict[str, datetime], dict[str, set[int]]]:
    """Return successful-send timestamps and historical template use by route."""
    latest_company: dict[str, datetime] = {}
    latest_recipient: dict[str, datetime] = {}
    used_templates: dict[str, set[int]] = defaultdict(set)
    for record in session.exec(select(SendRecord)).all():
        if (record.smtp_status or "").casefold() not in SUCCESS_STATUSES:
            continue
        sent_at = persisted_utc(record.sent_at or record.activity_date)
        if sent_at is None:
            continue
        company_id = record.company_id
        if company_id and (company_id not in latest_company or sent_at > latest_company[company_id]):
            latest_company[company_id] = sent_at
        recipient_email = _normal(record.recipient_email)
        if recipient_email and (recipient_email not in latest_recipient or sent_at > latest_recipient[recipient_email]):
            latest_recipient[recipient_email] = sent_at
        if record.template_id is not None:
            used_templates[company_id].add(record.template_id)
    return latest_company, latest_recipient, used_templates


def build_batch_plan(
    session: Session,
    *,
    customer_library: str,
    limit: int | None = None,
) -> dict:
    """Build a read-only plan; it never creates drafts, queue rows, or sends."""
    customer_library = (customer_library or "").strip().lower()
    if customer_library not in EMAIL_PLANNER_LIBRARIES:
        raise ValueError("Email planning requires the terminal or dealer customer library.")
    started_at = utc_now()
    policies = {
        policy.template_id: policy
        for policy in session.exec(select(TemplateRotationPolicy)).all()
    }
    templates = session.exec(
        select(EmailTemplate).where(
            EmailTemplate.is_active == True,
            EmailTemplate.template_type == "first_touch",
        )
    ).all()
    enabled_templates = [
        template for template in templates
        if (policy := policies.get(template.id)) and _matches_policy_placeholder(policy)
    ]
    enabled_templates.sort(key=lambda item: (policies[item.id].priority, item.id or 0))
    suppressions = session.exec(select(Suppression)).all()
    active_company_ids = set(session.exec(
        select(EmailDraft.company_id).where(EmailDraft.status.in_(ACTIVE_DRAFT_STATUSES))
    ).all())
    latest_company_success, latest_recipient_success, used_templates = _latest_successes(session)
    company_cooldown_cutoff = started_at - timedelta(hours=COMPANY_SUCCESS_COOLDOWN_HOURS)
    recipient_cooldown_cutoff = started_at - timedelta(days=RECIPIENT_SUCCESS_COOLDOWN_DAYS)

    planned: list[dict] = []
    total_newly_queueable = 0
    excluded: list[dict] = []
    already_arranged: list[dict] = []
    companies = session.exec(
        select(Company)
        .where(
            (func.lower(func.trim(func.coalesce(Company.customer_library, ""))) == "terminal")
            if customer_library == "terminal" else
            (func.lower(func.trim(func.coalesce(Company.customer_library, ""))) != "terminal")
        )
        .order_by(Company.priority, Company.name)
    ).all()
    for company in companies:
        if company.id in active_company_ids:
            already_arranged.append({"company_id": company.id, "company": company.name, "reason": "active_draft"})
            continue
        if _company_is_no_go(company):
            excluded.append({"company_id": company.id, "company": company.name, "reason": "no_go_company"})
            continue
        if (last_success := latest_company_success.get(company.id)) and last_success >= company_cooldown_cutoff:
            excluded.append({"company_id": company.id, "company": company.name, "reason": "company_48h_cooldown"})
            continue
        contact, contact_reason = _eligible_primary_contact(session, company.id, suppressions)
        if contact is None:
            excluded.append({"company_id": company.id, "company": company.name, "reason": contact_reason})
            continue
        if (last_success := latest_recipient_success.get(_normal(contact.email))) and last_success >= recipient_cooldown_cutoff:
            excluded.append({"company_id": company.id, "company": company.name, "reason": "recipient_7d_cooldown"})
            continue
        compatible = [
            template for template in enabled_templates
            if _matches_policy(company, policies[template.id]) and template_matches_target(template, company)
        ]
        unused = [template for template in compatible if template.id not in used_templates[company.id]]
        # Product tags are an advisory preference only: a non-match remains
        # eligible once country and customer-type tags have passed.
        unused.sort(key=lambda template: (product_match_score(template, company), policies[template.id].priority, template.id or 0))
        if not unused:
            excluded.append({
                "company_id": company.id,
                "company": company.name,
                "reason": "no_compatible_unused_template",
            })
            continue
        total_newly_queueable += 1
        if limit is not None and len(planned) >= limit:
            continue
        chosen = unused[0]
        planned.append({
            "company_id": company.id,
            "company": company.name,
            "contact_id": contact.id,
            "recipient_email": contact.email,
            "template_id": chosen.id,
            "template_name": chosen.name,
            "available_template_ids": [template.id for template in unused],
        })
    return {
        "ok": True,
        "read_only": True,
        "customer_library": customer_library,
        "generated_at": started_at.isoformat(),
        "summary": {
            "already_arranged": len(already_arranged),
            "newly_queueable": len(planned),
            "total_newly_queueable": total_newly_queueable,
            "plan_truncated": total_newly_queueable > len(planned),
            "excluded": len(excluded),
            "no_compatible_unused_template": sum(
                1 for item in excluded if item["reason"] == "no_compatible_unused_template"
            ),
            "company_48h_cooldown": sum(1 for item in excluded if item["reason"] == "company_48h_cooldown"),
            "recipient_7d_cooldown": sum(1 for item in excluded if item["reason"] == "recipient_7d_cooldown"),
            "source_success_locked": sum(1 for item in excluded if item["reason"] == "source_success_locked"),
            "enabled_rotation_templates": len(enabled_templates),
        },
        "planned": planned,
        "already_arranged": already_arranged,
        "excluded": excluded,
    }


def _matches_policy_placeholder(policy: TemplateRotationPolicy) -> bool:
    """Filter disabled/manual policies before per-company context is available."""
    return policy.is_enabled and policy.scope != SCOPE_MANUAL_ONLY


def update_rotation_policy(
    session: Session,
    template_id: int,
    *,
    scope: str,
    match_keywords: str | None,
    priority: int,
    is_enabled: bool,
) -> TemplateRotationPolicy:
    if scope not in VALID_SCOPES:
        raise ValueError("Unknown template rotation scope.")
    template = session.get(EmailTemplate, template_id)
    if template is None or template.template_type != "first_touch":
        raise ValueError("Template must be a first-touch template.")
    if template_references_field(template, "region"):
        raise ValueError("Templates that require company region cannot participate in automatic rotation.")
    policy = ensure_rotation_policy(session, template)
    policy.scope = scope
    policy.match_keywords = ",".join(_keyword_list(match_keywords)) or None
    policy.priority = max(0, priority)
    policy.is_enabled = is_enabled
    policy.updated_at = utc_now()
    session.add(policy)
    return policy
