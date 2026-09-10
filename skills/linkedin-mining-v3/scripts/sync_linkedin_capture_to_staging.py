#!/usr/bin/env python3
"""Idempotently index a LinkedIn capture task into a fixed, non-promoting staging SQLite DB.

The task directory remains the capture authority.  This program only reads its
JSON/JSONL/raw-page evidence and writes a rebuildable staging index; it never
opens, attaches, or references the official LinkedIn people master.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_COUNTRY_CODES = {"", "UK", "ZA", "US", "IE", "AU", "NZ", "CA"}
ALLOWED_CONTACT_REVIEW = {"captured", "review_needed"}
RUN_STATUS_MAP = {
    "in_progress": "in_progress",
    "complete": "complete",
    "awaiting_source": "awaiting_source",
    "source_page_load_blocked": "blocked",
}


class StagingError(ValueError):
    """Raised when legacy evidence cannot be represented without guessing."""


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as error:
        raise StagingError(f"required evidence file is missing: {path}") from error
    except json.JSONDecodeError as error:
        raise StagingError(f"invalid JSON evidence: {path}: {error}") from error


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise StagingError(f"invalid JSONL evidence: {path}:{number}: {error}") from error
        if not isinstance(value, dict):
            raise StagingError(f"JSONL row is not an object: {path}:{number}")
        rows.append(value)
    return rows


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def stable_run_id(task_dir: Path) -> str:
    digest = hashlib.sha256(str(task_dir.resolve()).lower().encode("utf-8")).hexdigest()[:24]
    return f"lmc_{digest}"


def default_database(task_dir: Path) -> Path:
    for parent in (task_dir, *task_dir.parents):
        if parent.name.lower() == "output":
            return parent / "_linkedin_mining_staging" / "linkedin_mining_staging.sqlite"
    raise StagingError("task directory must be inside an output directory; pass --db explicitly otherwise")


def require_direct_profile(value: Any, context: str) -> str:
    profile = str(value or "").strip()
    if not profile.startswith("https://www.linkedin.com/in/"):
        raise StagingError(f"{context}: profile is not a direct linkedin.com/in URL")
    return profile


def require_nonblank(value: Any, field: str, context: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise StagingError(f"{context}: missing required field {field}")
    return text


def page_number(value: Any, context: str) -> int:
    try:
        page = int(value)
    except (TypeError, ValueError) as error:
        raise StagingError(f"{context}: invalid source page {value!r}") from error
    if page < 1:
        raise StagingError(f"{context}: source page must be positive")
    return page


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS allowed_label (
  label_namespace TEXT NOT NULL,
  label_value TEXT NOT NULL,
  PRIMARY KEY (label_namespace, label_value)
);
CREATE TABLE IF NOT EXISTS capture_run (
  run_id TEXT PRIMARY KEY,
  legacy_task_dir TEXT NOT NULL UNIQUE,
  source_target TEXT NOT NULL,
  source_url TEXT NOT NULL,
  network_scope_json TEXT NOT NULL,
  run_status TEXT NOT NULL CHECK (run_status IN ('in_progress', 'complete', 'awaiting_source', 'blocked')),
  legacy_status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  synced_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS capture_page (
  run_id TEXT NOT NULL REFERENCES capture_run(run_id),
  page_number INTEGER NOT NULL CHECK (page_number >= 1),
  raw_file TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  result_count INTEGER NOT NULL CHECK (result_count >= 0),
  next_visible INTEGER NOT NULL CHECK (next_visible IN (0, 1)),
  terminal_reason TEXT,
  PRIMARY KEY (run_id, page_number)
);
CREATE TABLE IF NOT EXISTS raw_card (
  raw_card_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES capture_run(run_id),
  page_number INTEGER NOT NULL,
  card_order INTEGER NOT NULL CHECK (card_order >= 1),
  direct_profile_url TEXT NOT NULL,
  name TEXT NOT NULL,
  identity TEXT NOT NULL,
  location TEXT,
  action_label TEXT,
  raw_card_text TEXT NOT NULL,
  source_file TEXT NOT NULL,
  source_sha256 TEXT NOT NULL,
  UNIQUE (run_id, page_number, card_order),
  FOREIGN KEY (run_id, page_number) REFERENCES capture_page(run_id, page_number)
);
CREATE TABLE IF NOT EXISTS candidate (
  candidate_id TEXT PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES capture_run(run_id),
  legacy_contact_id TEXT NOT NULL,
  canonical_profile_url TEXT NOT NULL,
  name TEXT NOT NULL,
  identity TEXT NOT NULL,
  location TEXT,
  country_code TEXT NOT NULL CHECK (country_code IN ('', 'UK', 'ZA', 'US', 'IE', 'AU', 'NZ', 'CA')),
  promotion_state TEXT NOT NULL DEFAULT 'not_requested'
    CHECK (promotion_state IN ('not_requested', 'proposed', 'approved', 'promoted', 'rejected')),
  UNIQUE (run_id, canonical_profile_url),
  UNIQUE (run_id, legacy_contact_id)
);
CREATE TABLE IF NOT EXISTS candidate_label (
  candidate_id TEXT NOT NULL REFERENCES candidate(candidate_id),
  label_namespace TEXT NOT NULL,
  label_value TEXT NOT NULL,
  PRIMARY KEY (candidate_id, label_namespace),
  FOREIGN KEY (label_namespace, label_value) REFERENCES allowed_label(label_namespace, label_value)
);
CREATE TABLE IF NOT EXISTS profile_ledger (
  raw_card_id TEXT PRIMARY KEY REFERENCES raw_card(raw_card_id),
  candidate_id TEXT REFERENCES candidate(candidate_id),
  profile_match_status TEXT NOT NULL CHECK (profile_match_status = 'direct_same_card'),
  ledger_status TEXT NOT NULL CHECK (ledger_status IN ('joined', 'duplicate_profile_url'))
);
CREATE TABLE IF NOT EXISTS validation_event (
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id TEXT NOT NULL REFERENCES capture_run(run_id),
  raw_card_id TEXT,
  severity TEXT NOT NULL CHECK (severity IN ('error', 'warning')),
  code TEXT NOT NULL,
  detail TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promotion_batch (
  batch_id TEXT PRIMARY KEY,
  manifest_sha256 TEXT NOT NULL UNIQUE,
  status TEXT NOT NULL CHECK (status IN ('proposed', 'approved', 'rejected', 'promoted')),
  approved_by TEXT,
  approved_at TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promotion_item (
  batch_id TEXT NOT NULL REFERENCES promotion_batch(batch_id),
  candidate_id TEXT NOT NULL REFERENCES candidate(candidate_id),
  decision TEXT NOT NULL CHECK (decision IN ('approved', 'rejected')),
  decision_reason TEXT NOT NULL,
  PRIMARY KEY (batch_id, candidate_id)
);
"""

ALLOWED_LABELS = {
    "record_status": {"captured", "review_needed", "duplicate_profile_url", "rejected"},
    "profile_evidence": {"direct_same_card"},
    "country_resolution": {"explicit_country", "unresolved"},
    "run_status": {"in_progress", "complete", "awaiting_source", "blocked"},
    "promotion_state": {"not_requested", "proposed", "approved", "promoted", "rejected"},
}


def initialize_database(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    connection.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)", ("schema_version", "1"))
    for namespace, values in ALLOWED_LABELS.items():
        for value in values:
            connection.execute(
                "INSERT OR IGNORE INTO allowed_label(label_namespace, label_value) VALUES (?, ?)",
                (namespace, value),
            )


def upsert_label(connection: sqlite3.Connection, candidate_id: str, namespace: str, value: str) -> None:
    if value not in ALLOWED_LABELS.get(namespace, set()):
        raise StagingError(f"unapproved label {namespace}={value}")
    connection.execute(
        """INSERT INTO candidate_label(candidate_id, label_namespace, label_value) VALUES (?, ?, ?)
           ON CONFLICT(candidate_id, label_namespace) DO UPDATE SET label_value = excluded.label_value""",
        (candidate_id, namespace, value),
    )


def candidate_id(run_id: str, profile: str) -> str:
    return "cand_" + hashlib.sha256(f"{run_id}|{profile}".encode("utf-8")).hexdigest()[:24]


def raw_card_id(run_id: str, page: int, order: int) -> str:
    return f"{run_id}_p{page:03d}_{order:03d}"


def source_hash(task_dir: Path, relative_file: str) -> str:
    source_path = task_dir / relative_file.replace("/", "\\")
    if not source_path.exists():
        raise StagingError(f"raw evidence file is missing: {source_path}")
    return sha256_file(source_path)


def sync_task(task_dir: Path, database: Path) -> dict[str, int | str]:
    task_dir = task_dir.resolve()
    manifest = read_json(task_dir / "task_manifest.json")
    state = read_json(task_dir / "workflow_state.json")
    audit = read_json(task_dir / "coverage_audit.json")
    contacts = read_jsonl(task_dir / "contacts_master.jsonl")
    links = read_jsonl(task_dir / "source_profile_links.jsonl")
    duplicate_reviews = read_jsonl(task_dir / "review_needed.jsonl")

    status = str(state.get("status", "")).strip()
    if status not in RUN_STATUS_MAP:
        raise StagingError(f"legacy workflow status is not mapped: {status!r}")
    source_target = require_nonblank(manifest.get("source_target"), "source_target", "task manifest")
    source_url = require_nonblank(manifest.get("source_url"), "source_url", "task manifest")
    scope = manifest.get("network_scope")
    if not isinstance(scope, list) or not scope:
        raise StagingError("task manifest: network_scope must be a non-empty list")

    audit_by_page: dict[int, dict[str, Any]] = {}
    for item in audit.get("pages", []):
        if not isinstance(item, dict):
            raise StagingError("coverage audit contains a non-object page record")
        page = page_number(item.get("page"), "coverage audit")
        if page in audit_by_page:
            raise StagingError(f"coverage audit duplicates page {page}")
        audit_by_page[page] = item
    if audit_by_page and sorted(audit_by_page) != list(range(1, max(audit_by_page) + 1)):
        raise StagingError("coverage audit pages are not consecutive from page 1")

    contact_by_profile: dict[str, dict[str, Any]] = {}
    for index, contact in enumerate(contacts, start=1):
        context = f"contacts_master row {index}"
        profile = require_direct_profile(contact.get("linkedin_profile"), context)
        if profile in contact_by_profile:
            raise StagingError(f"{context}: duplicate profile in contacts_master")
        status_value = str(contact.get("review_status", "")).strip()
        if status_value not in ALLOWED_CONTACT_REVIEW:
            raise StagingError(f"{context}: legacy review status is not mapped: {status_value!r}")
        code = str(contact.get("country_code", "")).strip()
        if code not in ALLOWED_COUNTRY_CODES:
            raise StagingError(f"{context}: country code is not approved: {code!r}")
        page = page_number(contact.get("source_page"), context)
        if page not in audit_by_page:
            raise StagingError(f"{context}: source page {page} is absent from coverage audit")
        require_nonblank(contact.get("contact_id"), "contact_id", context)
        require_nonblank(contact.get("name"), "name", context)
        require_nonblank(contact.get("identity"), "identity", context)
        require_nonblank(contact.get("page_intro"), "page_intro", context)
        contact_by_profile[profile] = contact

    duplicate_links: set[tuple[int, str]] = set()
    for index, review in enumerate(duplicate_reviews, start=1):
        context = f"review_needed row {index}"
        if str(review.get("review_status", "")).strip() != "duplicate_profile_url":
            raise StagingError(f"{context}: review status is not mapped")
        duplicate_links.add((page_number(review.get("source_page"), context), require_direct_profile(review.get("linkedin_profile"), context)))

    links_by_page: dict[int, list[dict[str, Any]]] = {}
    for index, link in enumerate(links, start=1):
        context = f"source_profile_links row {index}"
        if str(link.get("profile_match_status", "")).strip() != "direct_same_card":
            raise StagingError(f"{context}: profile match status is not approved")
        profile = require_direct_profile(link.get("linkedin_profile"), context)
        page = page_number(link.get("source_page"), context)
        if page not in audit_by_page:
            raise StagingError(f"{context}: source page {page} is absent from coverage audit")
        require_nonblank(link.get("name"), "name", context)
        require_nonblank(link.get("identity"), "identity", context)
        require_nonblank(link.get("raw_card_text"), "raw_card_text", context)
        links_by_page.setdefault(page, []).append({**link, "linkedin_profile": profile})

    database.parent.mkdir(parents=True, exist_ok=True)
    run_id = stable_run_id(task_dir)
    connection = sqlite3.connect(database)
    with connection:
        connection.execute("PRAGMA foreign_keys = ON")
        initialize_database(connection)
        now = utc_now()
        connection.execute(
            """INSERT INTO capture_run(run_id, legacy_task_dir, source_target, source_url, network_scope_json, run_status, legacy_status, created_at, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET source_target=excluded.source_target, source_url=excluded.source_url,
                 network_scope_json=excluded.network_scope_json, run_status=excluded.run_status,
                 legacy_status=excluded.legacy_status, synced_at=excluded.synced_at""",
            (run_id, str(task_dir), source_target, source_url, json.dumps(scope, ensure_ascii=False), RUN_STATUS_MAP[status], status, now, now),
        )
        for page, item in audit_by_page.items():
            relative = str(item.get("raw_file", "")).replace("\\", "/")
            raw_path = task_dir / relative.replace("/", "\\")
            content_sha = sha256_file(raw_path) if raw_path.exists() else ""
            result_count = int(item.get("result_count", 0))
            if result_count > 0 and not content_sha:
                raise StagingError(f"coverage audit page {page}: missing raw evidence file {raw_path}")
            connection.execute(
                """INSERT INTO capture_page(run_id, page_number, raw_file, content_sha256, result_count, next_visible, terminal_reason)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(run_id, page_number) DO UPDATE SET raw_file=excluded.raw_file,
                     content_sha256=excluded.content_sha256, result_count=excluded.result_count,
                     next_visible=excluded.next_visible, terminal_reason=excluded.terminal_reason""",
                (run_id, page, relative, content_sha, result_count, int(bool(item.get("next_visible"))), audit.get("terminal_reason") if audit.get("terminal_page") == page else None),
            )
        for page, page_links in links_by_page.items():
            for order, link in enumerate(page_links, start=1):
                source_file = str(link.get("source_file", "")).replace("\\", "/")
                raw_id = raw_card_id(run_id, page, order)
                connection.execute(
                    """INSERT INTO raw_card(raw_card_id, run_id, page_number, card_order, direct_profile_url, name, identity, location, action_label, raw_card_text, source_file, source_sha256)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(raw_card_id) DO UPDATE SET direct_profile_url=excluded.direct_profile_url,
                         name=excluded.name, identity=excluded.identity, raw_card_text=excluded.raw_card_text,
                         source_file=excluded.source_file, source_sha256=excluded.source_sha256""",
                    (raw_id, run_id, page, order, link["linkedin_profile"], str(link["name"]).strip(), str(link["identity"]).strip(), None, None,
                     str(link["raw_card_text"]).strip(), source_file, source_hash(task_dir, source_file)),
                )
        for profile, contact in contact_by_profile.items():
            page = page_number(contact.get("source_page"), "contact")
            candidate = candidate_id(run_id, profile)
            code = str(contact.get("country_code", "")).strip()
            connection.execute(
                """INSERT INTO candidate(candidate_id, run_id, legacy_contact_id, canonical_profile_url, name, identity, location, country_code, promotion_state)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'not_requested')
                   ON CONFLICT(candidate_id) DO UPDATE SET legacy_contact_id=excluded.legacy_contact_id, name=excluded.name,
                     identity=excluded.identity, location=excluded.location, country_code=excluded.country_code""",
                (candidate, run_id, str(contact["contact_id"]).strip(), profile, str(contact["name"]).strip(), str(contact["identity"]).strip(), str(contact.get("location", "")).strip(), code),
            )
            upsert_label(connection, candidate, "record_status", str(contact["review_status"]).strip())
            upsert_label(connection, candidate, "profile_evidence", "direct_same_card")
            upsert_label(connection, candidate, "country_resolution", "explicit_country" if code else "unresolved")
            upsert_label(connection, candidate, "promotion_state", "not_requested")
            _ = page
        for page, page_links in links_by_page.items():
            for order, link in enumerate(page_links, start=1):
                profile = str(link["linkedin_profile"])
                candidate = candidate_id(run_id, profile) if profile in contact_by_profile else None
                ledger_status = "duplicate_profile_url" if (page, profile) in duplicate_links else "joined"
                if candidate is None and ledger_status != "duplicate_profile_url":
                    raise StagingError(f"source link page {page} has no contact or duplicate-review evidence: {profile}")
                connection.execute(
                    """INSERT INTO profile_ledger(raw_card_id, candidate_id, profile_match_status, ledger_status)
                       VALUES (?, ?, 'direct_same_card', ?)
                       ON CONFLICT(raw_card_id) DO UPDATE SET candidate_id=excluded.candidate_id, ledger_status=excluded.ledger_status""",
                    (raw_card_id(run_id, page, order), candidate, ledger_status),
                )
        connection.execute(
            "DELETE FROM validation_event WHERE run_id = ? AND code = 'sync_summary'", (run_id,)
        )
        connection.execute(
            "INSERT INTO validation_event(run_id, severity, code, detail, created_at) VALUES (?, 'warning', 'sync_summary', ?, ?)",
            (run_id, f"Indexed {len(links)} direct links, {len(contacts)} candidates, and {len(duplicate_reviews)} duplicate reviews.", now),
        )
        bad_foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if bad_foreign_keys:
            raise StagingError(f"staging foreign-key check failed: {bad_foreign_keys!r}")
    connection.close()
    return {
        "run_id": run_id,
        "database": str(database.resolve()),
        "pages": len(audit_by_page),
        "direct_links": len(links),
        "candidates": len(contacts),
        "duplicate_reviews": len(duplicate_reviews),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_dir", type=Path)
    parser.add_argument("--db", type=Path, help="Fixed staging SQLite path; defaults under the task's output directory")
    args = parser.parse_args()
    try:
        database = args.db.resolve() if args.db else default_database(args.task_dir.resolve())
        print(json.dumps(sync_task(args.task_dir, database), ensure_ascii=False, sort_keys=True))
    except StagingError as error:
        print(f"staging sync rejected: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
