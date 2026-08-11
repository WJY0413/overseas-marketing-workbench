from datetime import datetime

from app.models import Suppression
from app.time_utils import as_utc, utc_now


def is_active_suppression(item: Suppression, now: datetime | None = None) -> bool:
    """Return whether a permanent or unexpired suppression blocks sending."""
    expires_at = as_utc(item.expires_at)
    return expires_at is None or expires_at > (now or utc_now())
