from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from jinja2 import Environment, StrictUndefined, meta


UNRESOLVED_VARIABLE_RE = re.compile(r"{{\s*[^{}]+\s*}}")
INVALID_RENDER_VALUE_RE = re.compile(r"(?<![\w])(?:None|null|NULL|undefined|Undefined|NaN)(?![\w])")
EMPTY_FIELD_VALUES = {"", "none", "null", "undefined", "nan"}
SWITCH_TEMPLATE_GUIDANCE = (
    "Switch to another approved template that does not require the failed field(s)."
)


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
