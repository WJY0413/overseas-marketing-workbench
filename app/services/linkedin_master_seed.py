"""Build the packaged, operator-owned LinkedIn master seed."""
from __future__ import annotations

import ast
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse


CONNECTED_STATUS = "connection_attempted"
SEED_GREETING_NOTICE = "【内置样例】此为初始联系人库样例，请先按实际业务核对并重新生成招呼语后再使用。"


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _value(raw: dict, *keys: str, default: str = "-") -> str:
    for key in keys:
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return default


def _linkedin_profile(value: object) -> str:
    candidate = value
    if isinstance(candidate, str):
        text = candidate.strip()
        if text.startswith("{") and text.endswith("}"):
            try:
                candidate = ast.literal_eval(text)
            except (SyntaxError, ValueError):
                candidate = text
        else:
            candidate = text
    if isinstance(candidate, dict):
        candidate = candidate.get("value") or candidate.get("source_url") or ""
    profile = str(candidate or "").strip()
    parsed = urlparse(profile)
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() not in {"http", "https"}
        or not (host == "linkedin.com" or host.endswith(".linkedin.com"))
        or not parsed.path.casefold().startswith("/in/")
    ):
        return ""
    return profile


def _create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        PRAGMA foreign_keys = ON;
        CREATE TABLE people (
          person_id TEXT PRIMARY KEY, display_name TEXT NOT NULL, normalized_name TEXT NOT NULL,
          canonical_identity TEXT, normalized_identity TEXT, canonical_location TEXT,
          normalized_location TEXT, linkedin_profile TEXT, normalized_profile TEXT,
          canonical_source_record_id TEXT NOT NULL, source_record_count INTEGER NOT NULL,
          source_route_count INTEGER NOT NULL, canonical_json TEXT NOT NULL CHECK(json_valid(canonical_json)),
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE person_source_records (
          source_record_id TEXT PRIMARY KEY, person_id TEXT NOT NULL REFERENCES people(person_id) ON DELETE CASCADE,
          input_path TEXT NOT NULL, source_line_no INTEGER NOT NULL, source_target TEXT,
          source_row_id TEXT, raw_name TEXT NOT NULL, raw_identity TEXT, raw_location TEXT,
          raw_linkedin_profile TEXT, normalized_name TEXT NOT NULL, normalized_identity TEXT,
          normalized_location TEXT, normalized_profile TEXT, raw_json TEXT NOT NULL CHECK(json_valid(raw_json)),
          record_sha256 TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(input_path, source_line_no)
        );
        CREATE TABLE linkedin_connection_campaigns (
          campaign_id TEXT PRIMARY KEY NOT NULL, campaign_name TEXT NOT NULL, owner TEXT NOT NULL,
          purpose TEXT NOT NULL, source_path TEXT NOT NULL, email_nogo_scope TEXT NOT NULL,
          linkedin_policy TEXT NOT NULL, monitoring_cadence TEXT NOT NULL, campaign_status TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE person_tags (
          person_id TEXT NOT NULL REFERENCES people(person_id) ON DELETE CASCADE, tag TEXT NOT NULL,
          source_campaign_id TEXT NOT NULL REFERENCES linkedin_connection_campaigns(campaign_id) ON DELETE RESTRICT,
          active INTEGER NOT NULL DEFAULT 1, applied_at TEXT NOT NULL, removed_at TEXT,
          PRIMARY KEY(person_id, tag, source_campaign_id)
        );
        CREATE TABLE linkedin_connection_suppression_state (
          source_record_id TEXT PRIMARY KEY NOT NULL REFERENCES person_source_records(source_record_id),
          person_id TEXT NOT NULL REFERENCES people(person_id), contact_id TEXT NOT NULL,
          suppression_status TEXT NOT NULL, outreach_status TEXT NOT NULL, marked_at TEXT NOT NULL,
          suppressed_until TEXT NOT NULL, policy_code TEXT NOT NULL, policy_years INTEGER,
          reason TEXT NOT NULL, released_at TEXT, released_by TEXT, release_reason TEXT,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE linkedin_connection_suppression_events (
          event_id TEXT PRIMARY KEY NOT NULL,
          source_record_id TEXT NOT NULL REFERENCES linkedin_connection_suppression_state(source_record_id) ON DELETE CASCADE,
          person_id TEXT NOT NULL, contact_id TEXT NOT NULL, event_type TEXT NOT NULL, event_at TEXT NOT NULL,
          operator TEXT NOT NULL, prior_suppressed_until TEXT, new_suppressed_until TEXT,
          policy_code TEXT, policy_years INTEGER, reason TEXT NOT NULL,
          details_json TEXT NOT NULL CHECK(json_valid(details_json)), created_at TEXT NOT NULL
        );
        """
    )


def build_linkedin_master_seed(source_path: Path, target_path: Path, *, company_limit: int = 100) -> dict[str, int]:
    """Create a new 100-company default master from connected source contacts.

    The authority source is opened read-only.  Only minimal contact fields are
    copied; greeting content is replaced with a safe in-package notice.
    """
    if company_limit < 1:
        raise ValueError("company_limit must be positive")
    if source_path.resolve() == target_path.resolve():
        raise ValueError("target master must not replace the source master")
    source = sqlite3.connect(f"file:{source_path.resolve().as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    selected: list[dict] = []
    seen_companies: set[str] = set()
    try:
        rows = source.execute(
            """SELECT raw_name, raw_identity, raw_location, raw_linkedin_profile, raw_json
               FROM person_source_records
               WHERE lower(coalesce(json_extract(raw_json, '$.outreach_status'), ''))=?
               ORDER BY source_record_id""",
            (CONNECTED_STATUS,),
        )
        for row in rows:
            raw = json.loads(row["raw_json"])
            company = _value(raw, "resolved_company", "company_name", "current_company")
            if company.casefold() in seen_companies:
                continue
            profile = _linkedin_profile(raw.get("linkedin_profile")) or _linkedin_profile(row["raw_linkedin_profile"])
            if not profile:
                continue
            selected.append({
                "name": _value(raw, "name", default=str(row["raw_name"] or "-")),
                "identity": _value(raw, "identity", "role_title", "role_value", default=str(row["raw_identity"] or "-")),
                "location": _value(raw, "location", default=str(row["raw_location"] or "")),
                "company": company, "profile": profile,
            })
            seen_companies.add(company.casefold())
            if len(selected) == company_limit:
                break
    finally:
        source.close()
    if len(selected) != company_limit:
        raise ValueError(f"Expected {company_limit} connected companies; found {len(selected)}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        target_path.unlink()
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    connection = sqlite3.connect(target_path)
    try:
        _create_schema(connection)
        connection.execute(
            "INSERT INTO linkedin_connection_campaigns VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("packaged_seed_v501", "Packaged LinkedIn initial contacts", "Workbench", "Initial packaged master records", "package", "none", "manual_confirm_only", "none", "closed", now, now),
        )
        for index, item in enumerate(selected, 1):
            person_id = f"seed_person_{index:03d}"; source_id = f"seed_record_{index:03d}"
            name, identity, location, company, profile = item["name"], item["identity"], item["location"], item["company"], item["profile"]
            raw = {"name": name, "identity": identity, "location": location, "current_company": company,
                   "linkedin_profile": profile, "outreach_status": CONNECTED_STATUS,
                   "greeting_note": SEED_GREETING_NOTICE, "contact_id": source_id,
                   "seed_origin": "packaged_initial_master"}
            raw_json = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
            connection.execute(
                "INSERT INTO people VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (person_id, name, name.casefold(), identity, identity.casefold(), location, location.casefold(), profile, profile.casefold(), source_id, 1, 1, raw_json, now, now),
            )
            connection.execute(
                "INSERT INTO person_source_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (source_id, person_id, "packaged_initial_master", index, "packaged_initial_master", source_id, name, identity, location, profile, name.casefold(), identity.casefold(), location.casefold(), profile.casefold(), raw_json, _sha(raw_json), now),
            )
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("seed master validation failed")
        connection.commit()
    finally:
        connection.close()
    return {"company_count": len(selected), "record_count": len(selected)}
