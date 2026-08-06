import json
from html import unescape

from jinja2 import Environment, StrictUndefined
from sqlmodel import Session

from app.config import get_settings
from app.models import Company, Contact, EmailTemplate, SenderAccount
from app.services.render_validation import validate_rendered_message, validate_template_context
from app.services.signatures import append_signature, get_signature_config, get_signature_template, signature_context

env = Environment(undefined=StrictUndefined, autoescape=True)


def context_for(
    company: Company,
    contact: Contact,
    sender: SenderAccount | None = None,
    session: Session | None = None,
) -> dict[str, str | None]:
    signature = signature_context(get_signature_config(session), company=company, contact=contact, sender=sender)
    return {
        "first_name": contact.first_name or contact.full_name.split(" ")[0],
        "contact_name": contact.full_name,
        "position": contact.position,
        "email": contact.email,
        "company": company.name,
        "company_name": company.name,
        "country": company.country,
        "region": company.region,
        "company_type": company.company_type,
        "priority": company.priority,
        "source": company.source,
        "sender_name": get_settings().default_sender_name,
        "sender_email": sender.email if sender else signature["sender_email"],
        "signature_region": signature["region"],
        "signature_phone": signature["phone"],
    }


def render_template(
    template: EmailTemplate,
    company: Company,
    contact: Contact,
    session: Session | None = None,
    sender: SenderAccount | None = None,
) -> tuple[str, str, str | None, str]:
    context = context_for(company, contact, sender=sender, session=session)
    validate_template_context(template, context, environment=env)
    subject = unescape(env.from_string(template.subject).render(**context))
    body_html = env.from_string(template.body_html).render(**context)
    body_text = env.from_string(template.body_text).render(**context) if template.body_text else None
    body_html, body_text, rendered_signature = append_signature(
        session,
        body_html,
        body_text,
        company=company,
        contact=contact,
        sender=sender,
    )
    validate_rendered_message(
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        recipient_email=contact.email,
        cc_emails=template.cc_emails if template.cc_enabled else None,
    )
    signature_template = get_signature_template(session)
    snapshot = json.dumps(
        {
            "template_id": template.id,
            "template_name": template.name,
            "signature_template_id": signature_template.id if signature_template else None,
        },
        ensure_ascii=False,
    )
    return subject, body_html, body_text, snapshot
