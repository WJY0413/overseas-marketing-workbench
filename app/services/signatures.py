import json
import re
from copy import deepcopy
from html import escape

from jinja2 import Environment, StrictUndefined
from sqlmodel import Session
from sqlmodel import select

from app.models import Company, Contact, EmailTemplate, SenderAccount
from app.services.app_settings import get_app_setting, set_app_setting

SIGNATURE_SETTING_KEY = "email_signature_config"
SIGNATURE_TEMPLATE_TYPE = "signature"
DEFAULT_SIGNATURE_TEMPLATE_NAME = "Email Signature"
SIGNATURE_START = "<!-- bd-signature:start -->"
SIGNATURE_END = "<!-- bd-signature:end -->"
TEXT_SIGNATURE_START = "[[bd-signature:start]]"
TEXT_SIGNATURE_END = "[[bd-signature:end]]"
signature_env = Environment(undefined=StrictUndefined, autoescape=True)

DEFAULT_SIGNATURE_CONFIG = {
    "enabled": True,
    "sender_name": "Your Name",
    "title_prefix": "Regional Manager",
    "company_name": "Your Company",
    "default_region": "UK",
    "default_email": "sender@example.com",
    "default_phone": "+1 555 0100",
    "western_phone": "+1 555 0100",
    "southeast_asia_phone": "+1 555 0101",
    "address": "Your company address",
    "country_rules": [
        {
            "countries": ["United Kingdom", "UK", "Great Britain", "England", "Scotland", "Wales", "Ireland"],
            "region": "UK & IE",
            "phone": "+1 555 0100",
        },
        {
            "countries": ["France"],
            "region": "FR",
            "phone": "+1 555 0100",
        },
        {
            "countries": [
                "Thailand",
                "Vietnam",
                "Malaysia",
                "Singapore",
                "Indonesia",
                "Philippines",
                "Cambodia",
                "Laos",
                "Myanmar",
                "Brunei",
                "Timor-Leste",
            ],
            "region": "Southeast Asia",
            "phone": "+1 555 0101",
        },
        {
            "countries": [
                "Ireland",
                "Germany",
                "Spain",
                "Italy",
                "Netherlands",
                "Belgium",
                "Denmark",
                "Sweden",
                "Norway",
                "Finland",
                "Switzerland",
                "Austria",
                "Portugal",
                "United States",
                "USA",
                "Canada",
                "Australia",
                "New Zealand",
            ],
            "region": "Western Region",
            "phone": "+1 555 0100",
        },
    ],
    "sender_rules": [],
    "image_html": "",
}


def is_signature_template(template: EmailTemplate | None) -> bool:
    return bool(template and template.template_type == SIGNATURE_TEMPLATE_TYPE)


def mail_template_filter():
    return EmailTemplate.template_type != SIGNATURE_TEMPLATE_TYPE


def get_signature_template(session: Session | None) -> EmailTemplate | None:
    if session is None:
        return None
    return session.exec(
        select(EmailTemplate)
        .where(EmailTemplate.template_type == SIGNATURE_TEMPLATE_TYPE, EmailTemplate.is_active == True)
        .order_by(EmailTemplate.updated_at.desc())
    ).first()


def upsert_signature_template(
    session: Session,
    body_html: str,
    body_text: str | None = None,
    name: str = DEFAULT_SIGNATURE_TEMPLATE_NAME,
) -> EmailTemplate:
    current = get_signature_template(session)
    template_name = name.strip() or DEFAULT_SIGNATURE_TEMPLATE_NAME
    if (
        current is not None
        and current.name == template_name
        and current.body_html == body_html
        and (current.body_text or "") == (body_text or "")
    ):
        return current

    template = EmailTemplate(
        name=template_name,
        template_type=SIGNATURE_TEMPLATE_TYPE,
        subject="Signature",
        body_html=body_html,
        body_text=body_text,
        cc_enabled=False,
        cc_emails=None,
        is_active=True,
    )
    session.add(template)
    session.flush()
    old_templates = session.exec(
        select(EmailTemplate).where(
            EmailTemplate.template_type == SIGNATURE_TEMPLATE_TYPE,
            EmailTemplate.id != template.id,
        )
    ).all()
    for old_template in old_templates:
        old_template.is_active = False
        session.add(old_template)
    return template


def _normalize(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def _merged_config(raw_config: dict | None) -> dict:
    config = deepcopy(DEFAULT_SIGNATURE_CONFIG)
    if isinstance(raw_config, dict):
        for key, value in raw_config.items():
            if key in config:
                config[key] = value
    if not isinstance(config.get("country_rules"), list):
        config["country_rules"] = deepcopy(DEFAULT_SIGNATURE_CONFIG["country_rules"])
    if not isinstance(config.get("sender_rules"), list):
        config["sender_rules"] = []
    return config


def get_signature_config(session: Session | None = None) -> dict:
    if session is None:
        return _merged_config(None)
    raw = get_app_setting(session, SIGNATURE_SETTING_KEY, "")
    if not raw:
        return _merged_config(None)
    try:
        return _merged_config(json.loads(raw))
    except json.JSONDecodeError:
        return _merged_config(None)


def save_signature_config(session: Session, config: dict) -> dict:
    merged = _merged_config(config)
    set_app_setting(session, SIGNATURE_SETTING_KEY, json.dumps(merged, ensure_ascii=False, indent=2))
    return merged


def _country_match(rule: dict, country: str | None) -> bool:
    normalized_country = _normalize(country)
    if not normalized_country:
        return False
    for item in rule.get("countries") or []:
        normalized_rule = _normalize(str(item))
        if normalized_rule and (normalized_country == normalized_rule or normalized_rule in normalized_country):
            return True
    return False


def _sender_match(rule: dict, sender: SenderAccount | None) -> bool:
    sender_email = _normalize(sender.email if sender else "")
    rule_email = _normalize(str(rule.get("sender_email") or ""))
    return bool(sender_email and rule_email and sender_email == rule_email)


def signature_context(
    config: dict,
    company: Company | None = None,
    contact: Contact | None = None,
    sender: SenderAccount | None = None,
) -> dict[str, str]:
    country = company.country if company else None
    context = {
        "sender_name": str(config.get("sender_name") or "Your Name"),
        "title_prefix": str(config.get("title_prefix") or "Regional Manager"),
        "region": str(config.get("default_region") or "UK"),
        "company_name": str(config.get("company_name") or "Your Company"),
        "sender_email": sender.email if sender else str(config.get("default_email") or ""),
        "phone": str(config.get("default_phone") or ""),
        "address": str(config.get("address") or ""),
        "recipient_country": country or "",
        "recipient_email": contact.email if contact else "",
    }
    for rule in config.get("country_rules") or []:
        if _country_match(rule, country):
            context["region"] = str(rule.get("region") or context["region"])
            context["phone"] = str(rule.get("phone") or context["phone"])
            break
    for rule in config.get("sender_rules") or []:
        if _sender_match(rule, sender):
            context["sender_name"] = str(rule.get("sender_name") or context["sender_name"])
            context["region"] = str(rule.get("region") or context["region"])
            context["sender_email"] = str(rule.get("display_email") or context["sender_email"])
            context["phone"] = str(rule.get("phone") or context["phone"])
            break
    return context


def strip_signature_html(body_html: str) -> str:
    pattern = re.compile(
        rf"\s*{re.escape(SIGNATURE_START)}[\s\S]*?{re.escape(SIGNATURE_END)}\s*",
        flags=re.IGNORECASE,
    )
    return pattern.sub("", body_html or "").rstrip()


def strip_signature_text(body_text: str | None) -> str | None:
    if body_text is None:
        return None
    pattern = re.compile(
        rf"\s*{re.escape(TEXT_SIGNATURE_START)}[\s\S]*?{re.escape(TEXT_SIGNATURE_END)}\s*",
        flags=re.IGNORECASE,
    )
    return pattern.sub("", body_text).rstrip()


def _strip_signature_markers(value: str) -> str:
    return (value or "").replace(SIGNATURE_START, "").replace(SIGNATURE_END, "").strip()


def _strip_text_signature_markers(value: str) -> str:
    return (value or "").replace(TEXT_SIGNATURE_START, "").replace(TEXT_SIGNATURE_END, "").strip()


def _html_to_text(html: str) -> str:
    text = re.sub(r"(?i)<br\s*/?>", "\n", html)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)</div\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _render_signature_template(
    session: Session | None,
    context: dict[str, str],
) -> tuple[str, str | None] | None:
    template = get_signature_template(session)
    if template is None:
        return None
    html = signature_env.from_string(template.body_html).render(**context)
    text = signature_env.from_string(template.body_text).render(**context) if template.body_text else _html_to_text(html)
    return html, text


def render_signature_html(config: dict, context: dict[str, str], session: Session | None = None) -> str:
    rendered_template = _render_signature_template(session, context)
    if rendered_template is not None:
        signature_html, _ = rendered_template
        signature_html = _strip_signature_markers(signature_html)
        return (
            f"{SIGNATURE_START}\n"
            '<div class="bd-email-signature" style="margin-top:18px;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.45;color:#1f2937;">'
            f"{signature_html}"
            "</div>\n"
            f"{SIGNATURE_END}"
        )

    lines = [
        "Kind regards,",
        f"{context['sender_name']} | {context['title_prefix']} - {context['region']} | {context['company_name']}",
        f"{context['sender_email']} | TEL/WA: {context['phone']}",
        f"Address: {context['address']}",
    ]
    line_html = "\n".join(f"<div>{escape(line)}</div>" for line in lines)
    image_html = (config.get("image_html") or "").strip()
    if image_html:
        image_html = f'\n<div class="bd-signature-images">{image_html}</div>'
    return (
        f'{SIGNATURE_START}\n'
        '<div class="bd-email-signature" style="margin-top:18px;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.45;color:#1f2937;">'
        f"{line_html}{image_html}"
        "</div>\n"
        f"{SIGNATURE_END}"
    )


def render_signature_text(
    context: dict[str, str],
    session: Session | None = None,
) -> str:
    rendered_template = _render_signature_template(session, context)
    if rendered_template is not None:
        _, signature_text = rendered_template
        signature_text = _strip_text_signature_markers(signature_text or "")
        return "\n".join([TEXT_SIGNATURE_START, signature_text, TEXT_SIGNATURE_END])

    return "\n".join(
        [
            TEXT_SIGNATURE_START,
            "Kind regards,",
            f"{context['sender_name']} | {context['title_prefix']} - {context['region']} | {context['company_name']}",
            f"{context['sender_email']} | TEL/WA: {context['phone']}",
            f"Address: {context['address']}",
            TEXT_SIGNATURE_END,
        ]
    )


def append_signature(
    session: Session | None,
    body_html: str,
    body_text: str | None,
    company: Company | None = None,
    contact: Contact | None = None,
    sender: SenderAccount | None = None,
) -> tuple[str, str | None, dict[str, str] | None]:
    config = get_signature_config(session)
    if not config.get("enabled", True):
        return strip_signature_html(body_html), strip_signature_text(body_text), None

    context = signature_context(config, company=company, contact=contact, sender=sender)
    html = strip_signature_html(body_html)
    text = strip_signature_text(body_text)
    html = f"{html}\n{render_signature_html(config, context, session=session)}"
    if text is not None:
        text = f"{text}\n\n{render_signature_text(context, session=session)}"
    return html, text, context


def country_rules_to_text(config: dict) -> str:
    lines: list[str] = []
    for rule in config.get("country_rules") or []:
        countries = ", ".join(str(item).strip() for item in rule.get("countries", []) if str(item).strip())
        if countries:
            lines.append(f"{countries}|{rule.get('region', '')}|{rule.get('phone', '')}")
    return "\n".join(lines)


def sender_rules_to_text(config: dict) -> str:
    lines: list[str] = []
    for rule in config.get("sender_rules") or []:
        sender_email = str(rule.get("sender_email") or "").strip()
        if sender_email:
            lines.append(
                "|".join(
                    [
                        sender_email,
                        str(rule.get("display_email") or "").strip(),
                        str(rule.get("phone") or "").strip(),
                        str(rule.get("region") or "").strip(),
                        str(rule.get("sender_name") or "").strip(),
                    ]
                )
            )
    return "\n".join(lines)


def parse_country_rules_text(value: str) -> list[dict]:
    rules: list[dict] = []
    for line in (value or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) < 2:
            continue
        countries = [item.strip() for item in parts[0].split(",") if item.strip()]
        if not countries:
            continue
        rules.append(
            {
                "countries": countries,
                "region": parts[1],
                "phone": parts[2] if len(parts) > 2 else "",
            }
        )
    return rules


def parse_sender_rules_text(value: str) -> list[dict]:
    rules: list[dict] = []
    for line in (value or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split("|")]
        if not parts or "@" not in parts[0]:
            continue
        rules.append(
            {
                "sender_email": parts[0].lower(),
                "display_email": parts[1] if len(parts) > 1 else "",
                "phone": parts[2] if len(parts) > 2 else "",
                "region": parts[3] if len(parts) > 3 else "",
                "sender_name": parts[4] if len(parts) > 4 else "",
            }
        )
    return rules

