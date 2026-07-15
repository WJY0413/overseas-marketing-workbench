from pathlib import Path

import pandas as pd
from sqlmodel import Session, select

from app.config import get_settings
from app.models import Company, Contact, EmailDraft, EmailEvent, SenderAccount
from app.time_utils import display_dt, local_now


def export_sending_report(session: Session) -> Path:
    rows = []
    drafts = session.exec(select(EmailDraft).order_by(EmailDraft.created_at.desc())).all()
    for draft in drafts:
        company = session.get(Company, draft.company_id)
        contact = session.get(Contact, draft.contact_id)
        sender = session.get(SenderAccount, draft.sender_account_id) if draft.sender_account_id else None
        opens = session.exec(
            select(EmailEvent).where(EmailEvent.draft_id == draft.id, EmailEvent.event_type == "open")
        ).all()
        rows.append(
            {
                "company": company.name if company else "",
                "country": company.country if company else "",
                "priority": company.priority if company else "",
                "stage": company.status if company else "",
                "contact": contact.full_name if contact else "",
                "email": contact.email if contact else "",
                "sender": sender.email if sender else "",
                "subject": draft.subject,
                "status": draft.status,
                "scheduled_at": display_dt(draft.scheduled_at),
                "approved_at": display_dt(draft.approved_at),
                "sent_at": display_dt(draft.sent_at),
                "open_count": len(opens),
                "last_error": draft.error_message or "",
            }
        )
    reports_dir = get_settings().reports_dir
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"sending_report_{local_now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    pd.DataFrame(rows).to_excel(path, index=False)
    return path
