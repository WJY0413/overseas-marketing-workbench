from __future__ import annotations

import re
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from jinja2 import Environment, StrictUndefined, TemplateError, meta


UNRESOLVED_VARIABLE_RE = re.compile(r"{{\s*[^{}]+\s*}}")
INVALID_RENDER_VALUE_RE = re.compile(r"(?<![\w])(?:None|null|NULL|undefined|Undefined|NaN)(?![\w])")
EMPTY_FIELD_VALUES = {"", "none", "null", "undefined", "nan"}
SWITCH_TEMPLATE_GUIDANCE = (
    "Switch to another approved template that does not require the failed field(s)."
)
TEMPLATE_IMPORT_TEST_CONTEXT = {
    "first_name": "TemplateFirstName",
    "contact_name": "Template Contact",
    "position": "Template Position",
    "email": "template.contact@example.test",
    "company": "Template Company Ltd",
    "company_name": "Template Company Ltd",
    "country": "United Kingdom",
    "region": "England",
    "company_type": "Groundcare Dealer",
    "priority": "A",
    "source": "Template validation",
    "sender_name": "Template Sender",
    "sender_email": "template.sender@example.test",
    "signature_region": "UK",
    "signature_phone": "+44 20 0000 0000",
}


class TemplateFieldResolutionError(ValueError):
    """Raised when a template field cannot be safely resolved for delivery."""


def _is_empty_field_value(value: Any) -> bool:
    if value is None:
        return True
    return isinstance(value, str) and value.strip().casefold() in EMPTY_FIELD_VALUES


def template_field_resolution_issues(
    template: Any,
    context: Mapping[str, Any],
    *,
    environment: Environment | None = None,
) -> list[str]:
    parser = environment or Environment(undefined=StrictUndefined, autoescape=True)
    issues: list[str] = []
    components = (
        ("subject", getattr(template, "subject", None)),
        ("body_html", getattr(template, "body_html", None)),
        ("body_text", getattr(template, "body_text", None)),
    )
    for component_name, source in components:
        if not source:
            continue
        referenced_fields = sorted(meta.find_undeclared_variables(parser.parse(source)))
        for field_name in referenced_fields:
            if field_name not in context:
                issues.append(f"{component_name} field '{field_name}' is missing")
            elif _is_empty_field_value(context[field_name]):
                issues.append(f"{component_name} field '{field_name}' is empty")
    return list(dict.fromkeys(issues))


def validate_template_context(
    template: Any,
    context: Mapping[str, Any],
    *,
    environment: Environment | None = None,
) -> None:
    issues = template_field_resolution_issues(template, context, environment=environment)
    if issues:
        raise TemplateFieldResolutionError(
            f"Template field resolution failed: {'; '.join(issues)}. {SWITCH_TEMPLATE_GUIDANCE}"
        )


def validate_template_source(
    *,
    subject: str,
    body_html: str,
    body_text: str | None = None,
    environment: Environment | None = None,
) -> None:
    """Validate a prospective template before it is persisted.

    The test context intentionally gives every supported field a concrete,
    distinctive value. This catches malformed placeholders such as
    ``{company}``, which Jinja otherwise treats as ordinary text.
    """
    parser = environment or Environment(undefined=StrictUndefined, autoescape=True)
    candidate = SimpleNamespace(
        subject=subject,
        body_html=body_html,
        body_text=body_text,
    )
    try:
        validate_template_context(candidate, TEMPLATE_IMPORT_TEST_CONTEXT, environment=parser)
        rendered_subject = parser.from_string(subject).render(**TEMPLATE_IMPORT_TEST_CONTEXT)
        rendered_html = parser.from_string(body_html).render(**TEMPLATE_IMPORT_TEST_CONTEXT)
        rendered_text = (
            parser.from_string(body_text).render(**TEMPLATE_IMPORT_TEST_CONTEXT)
            if body_text
            else None
        )
    except TemplateError as exc:
        raise TemplateFieldResolutionError(
            f"Template syntax or test rendering failed: {exc}. {SWITCH_TEMPLATE_GUIDANCE}"
        ) from exc

    validate_rendered_message(
        subject=rendered_subject,
        body_html=rendered_html,
        body_text=rendered_text,
    )


def rendered_message_field_issues(
    *,
    subject: str | None,
    body_html: str | None,
    body_text: str | None,
    recipient_email: str | None = None,
    cc_emails: str | None = None,
) -> list[str]:
    issues: list[str] = []
    values = (
        ("subject", subject or ""),
        ("body_html", body_html or ""),
        ("body_text", body_text or ""),
        ("recipient_email", recipient_email or ""),
        ("cc_emails", cc_emails or ""),
    )
    for field_name, value in values:
        if field_name in {"subject", "recipient_email", "cc_emails"} and ("\r" in value or "\n" in value):
            issues.append(f"{field_name} contains a forbidden line break")
        if field_name in {"subject", "body_html", "body_text"} and ("{" in value or "}" in value):
            issues.append(f"{field_name} contains brace character(s) after rendering")
        unresolved = sorted(set(UNRESOLVED_VARIABLE_RE.findall(value)))
        if unresolved:
            issues.append(
                f"{field_name} contains unresolved template variable(s): {', '.join(unresolved)}"
            )
        invalid_values = sorted(set(INVALID_RENDER_VALUE_RE.findall(value)))
        if invalid_values:
            issues.append(
                f"{field_name} contains invalid rendered value(s): {', '.join(invalid_values)}"
            )
    return list(dict.fromkeys(issues))


def validate_rendered_message(
    *,
    subject: str | None,
    body_html: str | None,
    body_text: str | None,
    recipient_email: str | None = None,
    cc_emails: str | None = None,
) -> None:
    issues = rendered_message_field_issues(
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        recipient_email=recipient_email,
        cc_emails=cc_emails,
    )
    if issues:
        raise TemplateFieldResolutionError(
            f"Template field resolution failed: {'; '.join(issues)}. {SWITCH_TEMPLATE_GUIDANCE}"
        )
