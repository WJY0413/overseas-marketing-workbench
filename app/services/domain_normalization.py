"""Shared historical domain normalization, extracted unchanged from bd_master_sync."""
from urllib.parse import urlsplit


def normalize_domain(value: object) -> str:
    raw = str(value or "").strip().casefold()
    if not raw:
        return ""
    parsed = urlsplit(raw if "://" in raw else f"//{raw}")
    host = (parsed.hostname or parsed.path.split("/", 1)[0]).strip(".")
    return host[4:] if host.startswith("www.") else host
