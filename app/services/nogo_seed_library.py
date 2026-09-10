"""Import the optional internal NO-GO seed without replacing local CRM data."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from sqlmodel import Session, select

from app.models import Suppression


def _seed_connection(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def import_internal_nogo_seed(session: Session, seed_path: Path) -> dict[str, int]:
    """Idempotently add an internal seed when the package explicitly contains it.

    The seed holds only permanent exact-email suppressions and confirmed
    customer domain suppressions. Existing local records always win; no local
    data is removed and no CRM company rows are created.
    """
    result = {"email_suppressions_created": 0, "domain_suppressions_created": 0}
    if not seed_path.is_file():
        return result

    source = _seed_connection(seed_path)
    try:
        email_rows = source.execute(
            "SELECT email, reason FROM nogo_suppressions ORDER BY email"
        ).fetchall()
        domain_rows = source.execute(
            "SELECT domain, reason FROM nogo_domains ORDER BY domain"
        ).fetchall()
    finally:
        source.close()

    existing_suppressions = {
        str(email).strip().casefold()
        for email in session.exec(select(Suppression.email)).all()
        if str(email).strip()
    }
    for raw_email, raw_reason in email_rows:
        email = str(raw_email or "").strip().casefold()
        if not email or email in existing_suppressions:
            continue
        session.add(Suppression(email=email, reason=str(raw_reason or "internal_nogo_seed")))
        existing_suppressions.add(email)
        result["email_suppressions_created"] += 1
    for raw_domain, raw_reason in domain_rows:
        domain = str(raw_domain or "").strip().casefold().lstrip("@")
        email = f"@{domain}"
        if not domain or "." not in domain or email in existing_suppressions:
            continue
        session.add(Suppression(email=email, reason=str(raw_reason or "confirmed_customer_domain_nogo")))
        existing_suppressions.add(email)
        result["domain_suppressions_created"] += 1

    if any(result.values()):
        session.commit()
    return result
