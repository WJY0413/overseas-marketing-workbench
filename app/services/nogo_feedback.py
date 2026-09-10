"""Minimal operator-triggered NO-GO feedback email; it never enters the marketing queue."""
from __future__ import annotations

import json
import os
import smtplib
import ssl
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from sqlmodel import Session, select

from app.config import get_settings
from app.models import Company, Contact, SenderAccount, Suppression
from app.services.no_go_companies import is_no_go_company
from app.services.secrets import decrypt_secret
from app.services.suppression import is_active_suppression

RECIPIENT = os.environ.get("WORKBENCH_NOGO_FEEDBACK_TO", "").strip()
from app.services.queue import SHARED_RECIPIENT_DOMAINS

SHARED_EMAIL_DOMAINS = SHARED_RECIPIENT_DOMAINS


def send_nogo_feedback(session: Session) -> dict[str, object]:
    if not RECIPIENT:
        raise ValueError('Configure WORKBENCH_NOGO_FEEDBACK_TO before sending feedback.')
    sender = session.exec(select(SenderAccount).where(SenderAccount.is_active == True).order_by(SenderAccount.id)).first()
    if sender is None:
        raise ValueError("未配置可用发件邮箱，无法反馈 NO-GO 增量。")
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
    directory = get_settings().data_dir / "nogo_feedback"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"nogo-feedback-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.write_text(json.dumps({"schema": "nogo-feedback-v1", "records": payload}, ensure_ascii=False, indent=2), encoding="utf-8")
    if get_settings().dry_run_email:
        return {"status": "dry_run", "count": len(payload), "sender": sender.email, "attachment": str(path)}
    password = decrypt_secret(sender.smtp_password_encrypted)
    if not password:
        raise ValueError("默认第一发件邮箱没有可用 SMTP 密码。")
    message = EmailMessage()
    message["From"] = f"{sender.name} <{sender.email}>"
    message["To"] = RECIPIENT
    message["Subject"] = f"NO-GO feedback: {len(payload)} minimal records"
    message.set_content("Attached is a minimal NO-GO feedback package. It separates company NO-GO records from exact bounced-email suppressions; it contains no contacts, email history, credentials, or CRM history.")
    message.add_attachment(path.read_bytes(), maintype="application", subtype="json", filename=path.name)
    smtp = smtplib.SMTP_SSL(sender.smtp_host, sender.smtp_port, timeout=30, context=ssl.create_default_context()) if sender.smtp_port == 465 else smtplib.SMTP(sender.smtp_host, sender.smtp_port, timeout=30)
    with smtp:
        if sender.smtp_port != 465: smtp.starttls(context=ssl.create_default_context())
        smtp.login(sender.smtp_username or sender.email, password)
        smtp.send_message(message)
    return {"status": "sent", "count": len(payload), "sender": sender.email, "attachment": str(path)}
