from datetime import datetime

from app.models import Suppression
from app.time_utils import as_utc, utc_now


def is_active_suppression(item: Suppression, now: datetime | None = None) -> bool:
    """Return whether a permanent or unexpired suppression blocks sending."""
    expires_at = as_utc(item.expires_at)
    return expires_at is None or expires_at > (now or utc_now())


def suppression_matches_email(item: Suppression, email: str | None) -> bool:
    """Match an active exact-email or whole-domain suppression rule."""
    normalized_email = (email or "").strip().casefold()
    rule = (item.email or "").strip().casefold()
    if not normalized_email or not rule or not is_active_suppression(item):
        return False
    return rule == normalized_email or (rule.startswith("@") and normalized_email.endswith(rule))


def active_suppression_rules(items: list[Suppression], now: datetime | None = None) -> set[str]:
    """Normalize active rules once per request, without a stale cross-request cache."""
    checked_at = now or utc_now()
    return {(item.email or "").strip().casefold() for item in items
            if (item.email or "").strip() and is_active_suppression(item, checked_at)}


def email_matches_suppression_rules(rules: set[str], email: str | None) -> bool:
    normalized = (email or "").strip().casefold()
    if not normalized:
        return False
    return normalized in rules or any(
        normalized[index:] in rules for index, char in enumerate(normalized) if char == "@"
    )
