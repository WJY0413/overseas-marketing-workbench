"""Dual-counter, review-first no-reply follow-up cadence."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import Company, Contact, EmailDraft, EmailEvent, EmailTemplate, SendRecord
from app.services.app_settings import get_app_setting, set_app_setting
from app.services.render_validation import rendered_message_field_issues
from app.services.signatures import get_signature_template, is_signature_template
from app.services.templates import render_template
from app.time_utils import utc_now


CADENCE_SETTING_KEY = "followup_dual_counter_v1"
CONTACT_STEP_HOURS = 24
COMPANY_SWITCH_MIN_HOURS = 48
DEFAULT_PRIORITIES = ("A", "B", "C")
STOPPED_COMPANY_STATUSES = {"replied", "unsubscribed", "paused", "blacklisted"}
OPEN_DRAFT_STATUSES = ("draft", "pending_review", "approved", "queued")


@dataclass(frozen=True)
class FollowUpCadence:
    enabled: bool = False
    contact_step_hours: int = CONTACT_STEP_HOURS
    company_switch_min_hours: int = COMPANY_SWITCH_MIN_HOURS
    template_id: int | None = None
    priorities: tuple[str, ...] = DEFAULT_PRIORITIES

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FollowUpDecision:
    due_at: datetime
    contact_success_count: int
    contact_due_at: datetime | None
    company_switch_applied: bool
    company_switch_due_at: datetime | None


def _positive_int(value: object, field_name: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a whole number.") from exc
    if parsed <= 0:
        raise ValueError(f"{field_name} must be greater than zero.")
    return parsed


def _normalise_priorities(value: object) -> tuple[str, ...]:
    if isinstance(value, str):
        parts = value.split(",")
    elif isinstance(value, (list, tuple)):
        parts = value
    else:
        raise ValueError("Priorities must be a comma-separated list.")
    priorities = tuple(dict.fromkeys(str(item).strip().upper() for item in parts if str(item).strip()))
    if not priorities:
        raise ValueError("At least one company priority is required.")
    return priorities


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def get_followup_cadence(session: Session) -> FollowUpCadence:
    raw = get_app_setting(session, CADENCE_SETTING_KEY, "")
    if not raw:
        return FollowUpCadence()
    try:
        payload = json.loads(raw)
        template_id = payload.get("template_id")
        return FollowUpCadence(
            enabled=bool(payload.get("enabled", False)),
            contact_step_hours=_positive_int(payload.get("contact_step_hours", CONTACT_STEP_HOURS), "Contact step"),
            company_switch_min_hours=_positive_int(
                payload.get("company_switch_min_hours", COMPANY_SWITCH_MIN_HOURS), "Company switch minimum"
            ),
            template_id=_positive_int(template_id, "Template ID") if template_id is not None else None,
            priorities=_normalise_priorities(payload.get("priorities", DEFAULT_PRIORITIES)),
        )
    except (TypeError, ValueError, KeyError, json.JSONDecodeError):
        return FollowUpCadence()


def save_followup_cadence(
    session: Session,
    *,
    enabled: bool,
    template_id: int | None,
    priorities: str | list[str] | tuple[str, ...],
) -> FollowUpCadence:
    normalised_priorities = _normalise_priorities(priorities)
    if enabled:
        if template_id is None:
            raise ValueError("Select an active follow-up template before enabling the cadence.")
        template = session.get(EmailTemplate, _positive_int(template_id, "Template ID"))
        if template is None or not template.is_active or template.template_type != "follow_up":
            raise ValueError("Select an active follow-up template before enabling the cadence.")
        if is_signature_template(template):
            raise ValueError("Signature templates cannot be used for follow-up cadence.")
    cadence = FollowUpCadence(
        enabled=enabled,
        template_id=_positive_int(template_id, "Template ID") if template_id is not None else None,
        priorities=normalised_priorities,
    )
    set_app_setting(session, CADENCE_SETTING_KEY, json.dumps(cadence.as_dict(), ensure_ascii=False, separators=(",", ":")))
    return cadence


def _decision_for_contact(records: list[SendRecord], contact_id: int, now: datetime) -> FollowUpDecision:
    """Apply the contact counter and, only when changing contact, the company gate."""
    current_time = _as_utc(now)
    ordered = sorted(records, key=lambda record: (_as_utc(record.sent_at), record.id or 0))
    contact_records = [record for record in ordered if record.contact_id == contact_id]
    contact_due_at = None
    due_at = current_time
    if contact_records:
        contact_due_at = _as_utc(contact_records[-1].sent_at) + timedelta(hours=len(contact_records) * CONTACT_STEP_HOURS)
        due_at = max(due_at, contact_due_at)

    latest_company_record = ordered[-1] if ordered else None
    switched = latest_company_record is not None and latest_company_record.contact_id != contact_id
    company_switch_due_at = None
    if switched:
        company_switch_due_at = _as_utc(latest_company_record.sent_at) + timedelta(hours=COMPANY_SWITCH_MIN_HOURS)
        due_at = max(due_at, company_switch_due_at)

    return FollowUpDecision(
        due_at=due_at,
        contact_success_count=len(contact_records),
        contact_due_at=contact_due_at,
        company_switch_applied=switched,
        company_switch_due_at=company_switch_due_at,
    )


def _successful_history_by_company(session: Session) -> dict[int, list[SendRecord]]:
    records = session.exec(
        select(SendRecord)
        .where(func.lower(SendRecord.smtp_status) == "sent", SendRecord.sent_at != None)
        .order_by(SendRecord.company_id, SendRecord.sent_at, SendRecord.id)
    ).all()
    history: dict[int, list[SendRecord]] = {}
    for record in records:
        history.setdefault(record.company_id, []).append(record)
    return history


def _has_open_draft_for_company(session: Session, company_id: int) -> bool:
    return session.exec(
        select(EmailDraft).where(
            EmailDraft.company_id == company_id,
            EmailDraft.status.in_(OPEN_DRAFT_STATUSES),
        )
    ).first() is not None


def scan_staged_followups(session: Session) -> dict[str, int] | None:
    """Create at most one pending-review draft per eligible company from sendrecord history."""
    cadence = get_followup_cadence(session)
    if not cadence.enabled or cadence.template_id is None:
        return None
    return generate_dual_counter_followups(
        session,
        template_id=cadence.template_id,
        priorities=cadence.priorities,
    )


def generate_dual_counter_followups(
    session: Session,
    *,
    template_id: int,
    priorities: str | list[str] | tuple[str, ...],
    limit: int | None = None,
) -> dict[str, int]:
    """Create a manually capped, review-only batch using the dual-counter policy."""
    if limit is not None and limit <= 0:
        raise ValueError("Follow-up batch limit must be greater than zero.")
    normalised_priorities = _normalise_priorities(priorities)
    template = session.get(EmailTemplate, template_id)
    if template is None or not template.is_active or template.template_type != "follow_up" or is_signature_template(template):
        return {"followup_drafts": 0}

    generated = 0
    now = utc_now()
    eligible: list[tuple[int, datetime, int, Company, Contact, FollowUpDecision]] = []
    priority_rank = {"A": 0, "B": 1, "C": 2}
    for company_id, records in _successful_history_by_company(session).items():
        latest_record = records[-1]
        company = session.get(Company, company_id)
        contact = session.get(Contact, latest_record.contact_id)
        if company is None or contact is None or contact.status != "active":
            continue
        if company.status in STOPPED_COMPANY_STATUSES or company.priority not in normalised_priorities:
            continue
        if _has_open_draft_for_company(session, company.id):
            continue

        decision = _decision_for_contact(records, contact.id, now)
        if decision.due_at > now:
            continue
        eligible.append((priority_rank.get(company.priority, 99), decision.due_at, company.id, company, contact, decision))

    selected = sorted(eligible, key=lambda item: (item[0], item[1], item[2]))
    if limit is not None:
        selected = selected[:limit]
    for _, _, _, company, contact, decision in selected:
        signature_template = get_signature_template(session)
        try:
            subject, html, text, snapshot = render_template(template, company, contact, session=session)
            render_issues = rendered_message_field_issues(
                subject=subject,
                body_html=html,
                body_text=text,
                recipient_email=contact.email,
                cc_emails=template.cc_emails if template.cc_enabled else None,
            )
            render_error = "; ".join(render_issues) if render_issues else None
        except Exception as exc:
            subject, html, text = template.subject, template.body_html, template.body_text
            snapshot = json.dumps({"template_id": template.id, "render_error": str(exc)}, ensure_ascii=False)
            render_error = f"Template render error: {exc}"
        draft = EmailDraft(
            company_id=company.id,
            contact_id=contact.id,
            template_id=template.id,
            signature_template_id=signature_template.id if signature_template else None,
            subject=subject,
            body_html=html,
            body_text=text,
            cc_emails=template.cc_emails if template.cc_enabled else None,
            status="pending_review",
            follow_up_step=decision.contact_success_count,
            template_snapshot=snapshot,
            error_message=render_error,
        )
        session.add(draft)
        session.flush()
        session.add(
            EmailEvent(
                draft_id=draft.id,
                event_type="followup_dual_counter_pending_review",
                metadata_json=json.dumps(
                    {
                        "policy": "dual_counter_v1",
                        "due_at": decision.due_at.isoformat(),
                        "contact_success_count": decision.contact_success_count,
                        "contact_due_at": decision.contact_due_at.isoformat() if decision.contact_due_at else None,
                        "company_switch_applied": decision.company_switch_applied,
                        "company_switch_due_at": (
                            decision.company_switch_due_at.isoformat() if decision.company_switch_due_at else None
                        ),
                    },
                    ensure_ascii=False,
                ),
            )
        )
        company.status = "follow_up_due"
        company.next_follow_up_at = decision.due_at
        company.updated_at = now
        session.add(company)
        generated += 1
    session.commit()
    result = {"followup_drafts": generated}
    if limit is not None:
        result.update({"eligible": len(eligible), "limit": limit})
    return result
