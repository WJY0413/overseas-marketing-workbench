"""Deterministic template target-tag matching.

Country and customer-type tags are delivery gates. Product tags are deliberately
advisory: they influence planner preference but never prevent marketing.
"""

from __future__ import annotations

import re

from app.models import Company, EmailTemplate

GENERAL_TAGS = {"", "通用", "general", "all", "*"}
TYPE_AGENT = "代理"
TYPE_TERMINAL = "终端"
TYPE_GOLF = "终端-高尔夫球场"
TYPE_SPORTS = "终端-运动场"
TYPE_OTHER = "终端-其他"
PRODUCT_LINE_MARKER = "划线机"
PRODUCT_MOWER = "割草机"
PRODUCT_GENERAL = "通用"


def _normal(value: object) -> str:
    return " ".join(str(value or "").casefold().replace("－", "-").split())


def _tags(value: str | None) -> set[str]:
    return {_normal(item) for item in re.split(r"[,，;；\n]", value or "") if _normal(item)} or {_normal("通用")}


def normalize_tag_text(value: str | None) -> str:
    """Store a readable deduplicated tag list; blank means general."""
    items: list[str] = []
    seen: set[str] = set()
    for item in re.split(r"[,，;；\n]", value or ""):
        cleaned = " ".join(item.strip().split())
        marker = _normal(cleaned)
        if marker and marker not in seen:
            items.append(cleaned)
            seen.add(marker)
    return ", ".join(items) if items else "通用"


def company_target_type(company: Company) -> str | None:
    value = _normal(company.company_type)
    if not value:
        return None
    if any(token in value for token in ("代理", "代理商", "dealer", "distributor", "agent", "reseller")):
        return TYPE_AGENT
    if any(token in value for token in ("高尔夫", "golf")):
        return TYPE_GOLF
    if any(token in value for token in ("运动场", "体育", "stadium", "sport", "pitch", "football")):
        return TYPE_SPORTS
    if any(token in value for token in ("终端", "end user", "customer", "venue", "facility", "contractor")):
        return TYPE_OTHER
    return None


def _matches_country(template: EmailTemplate, company: Company) -> bool:
    tags = _tags(template.target_countries)
    if tags & {_normal(item) for item in GENERAL_TAGS}:
        return True
    return bool(company.country and _normal(company.country) in tags)


def _matches_type(template: EmailTemplate, company: Company) -> bool:
    tags = _tags(template.target_types)
    if tags & {_normal(item) for item in GENERAL_TAGS}:
        return True
    target_type = company_target_type(company)
    if target_type is None:
        return False
    normalized_type = _normal(target_type)
    return normalized_type in tags or (normalized_type.startswith(_normal(TYPE_TERMINAL) + "-") and _normal(TYPE_TERMINAL) in tags)


def template_target_issues(template: EmailTemplate, company: Company) -> list[str]:
    """Return hard delivery-gate issues for country/type tags only."""
    issues: list[str] = []
    if not _matches_country(template, company):
        issues.append(f"Template country tags ({template.target_countries}) do not match company country ({company.country or 'missing'}).")
    if not _matches_type(template, company):
        issues.append(f"Template type tags ({template.target_types}) do not match company type ({company.company_type or 'missing'}).")
    return issues


def template_matches_target(template: EmailTemplate, company: Company) -> bool:
    return not template_target_issues(template, company)


def product_match_score(template: EmailTemplate, company: Company) -> int:
    """Advisory product match: lower is preferred, but it can never exclude."""
    tags = _tags(template.product_tags)
    context = _normal(" ".join(str(value or "") for value in (company.name, company.company_type, company.company_intro, company.notes)))
    detected = PRODUCT_LINE_MARKER if any(token in context for token in ("划线", "line marking", "linemarking", "pitch marking")) else (
        PRODUCT_MOWER if any(token in context for token in ("割草", "mower", "mowing", "turf", "golf")) else None
    )
    if detected and _normal(detected) in tags:
        return 0
    if _normal(PRODUCT_GENERAL) in tags:
        return 1
    return 2
