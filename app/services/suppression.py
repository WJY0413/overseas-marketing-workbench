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
