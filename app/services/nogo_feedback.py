"""Read-only NO-GO feedback snapshot for download; no mail transport."""
from __future__ import annotations

from sqlmodel import Session, select

from app.models import Company, Suppression
from app.services.no_go_companies import is_no_go_company
from app.services.suppression import is_active_suppression
from app.services.queue import SHARED_RECIPIENT_DOMAINS

SHARED_EMAIL_DOMAINS = SHARED_RECIPIENT_DOMAINS


def build_nogo_feedback(session: Session) -> dict[str, object]:
    company_rows = [company for company in session.exec(select(Company)).all() if is_no_go_company(company)]
    email_rows = [item for item in session.exec(select(Suppression)).all() if is_active_suppression(item) and not item.email.strip().startswith("@") and "bounce" in (item.reason or "").lower()]
    domain_bounces: dict[str, set[str]] = {}
    for item in email_rows:
        domain = item.email.rsplit("@", 1)[-1].casefold() if "@" in item.email else ""
        if domain and domain not in SHARED_EMAIL_DOMAINS:
            domain_bounces.setdefault(domain, set()).add(item.email.casefold())
    domain_rows = [{"record_type": "domain_nogo", "domain": domain, "reason": "auto_escalated_three_distinct_bounced_emails", "suppressed_email_count": len(emails)} for domain, emails in domain_bounces.items() if len(emails) >= 3]
    payload = (
        [{"record_type": "company_nogo", "company_name": row.name, "domain": row.domain, "reason": row.blacklist_reason or row.status, "source": row.source or "local", "updated_at": row.updated_at.isoformat()} for row in company_rows]
        + [{"record_type": "email_suppression", "email": row.email, "reason": row.reason, "expires_at": row.expires_at.isoformat() if row.expires_at else None, "created_at": row.created_at.isoformat()} for row in email_rows]
        + domain_rows
    )
    return {"schema": "nogo-feedback-v1", "records": payload}
