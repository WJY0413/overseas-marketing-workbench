from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.config import get_settings


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def local_now() -> datetime:
    return utc_now().astimezone(ZoneInfo(get_settings().app_timezone))


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo(get_settings().app_timezone))
    return value.astimezone(timezone.utc)


def persisted_utc(value: datetime | None) -> datetime | None:
    """Read Workbench persisted event timestamps using their UTC storage contract.

    SQLite strips timezone offsets from DateTime values.  Workbench event rows
    have always been written from ``utc_now()``, so a legacy naive value is UTC
    rather than an operator-local timestamp.  Keep this separate from
    ``as_utc()``, which is for local/user-entered datetimes.
    """
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def display_dt(value: datetime | None) -> str:
    if value is None:
        return "-"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo(get_settings().app_timezone)).strftime("%Y-%m-%d %H:%M")


def local_day_bounds_utc(value: datetime | None = None) -> tuple[datetime, datetime]:
    timezone_info = ZoneInfo(get_settings().app_timezone)
    local_value = (value or utc_now()).astimezone(timezone_info)
    start_local = local_value.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)
