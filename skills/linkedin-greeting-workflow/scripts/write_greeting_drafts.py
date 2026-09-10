#!/usr/bin/env python3
"""Write QA-backed LinkedIn greeting drafts into a chosen local raw library."""

from __future__ import annotations

import argparse
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def first_name(value: str) -> str:
    parts = value.strip().split()
    return parts[0] if parts else "there"


def greeting(row: sqlite3.Row) -> tuple[str, str, str]:
    if row["terminal_category"] != "organization_route_extracted":
        return "review", "", "no_qa_backed_strict_company_route"
    role = str(row["role_text"] or "").strip()
    company = str(row["company_name"] or "").strip()
    if not role or not company:
        return "review", "", "missing_verified_role_or_company"
    text = (
        f"Hi {first_name(str(row['person_name']))}, I came across your profile and noticed "
        f"your work as {role} at {company}. It would be great to connect."
    )
    return "draft_ready", text, ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path, help="Explicit LinkedIn raw-library SQLite path")
    args = parser.parse_args()
    database = args.database.resolve()
    if not database.is_file():
        raise SystemExit(f"Specified library does not exist: {database}")
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        connection.execute(
            """CREATE TABLE IF NOT EXISTS linkedin_greeting_draft (
                 subject_id TEXT PRIMARY KEY REFERENCES parse_subject(subject_id),
                 linkedin_profile TEXT NOT NULL,
                 person_name TEXT NOT NULL,
                 role_text TEXT,
                 company_name TEXT,
                 greeting_status TEXT NOT NULL CHECK(greeting_status IN ('draft_ready', 'review')),
                 greeting_draft TEXT NOT NULL DEFAULT '',
                 review_reason TEXT NOT NULL DEFAULT '',
                 parse_input_hash TEXT NOT NULL,
                 generated_at TEXT NOT NULL
               )"""
        )
        rows = connection.execute(
            """SELECT subject_id, person_name, role_text, company_name, linkedin_profile,
                      terminal_category, input_hash
               FROM parsed_contacts_effective ORDER BY person_name, subject_id"""
        ).fetchall()
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        ready = review = 0
        for row in rows:
            status, text, reason = greeting(row)
            ready += status == "draft_ready"
            review += status == "review"
            connection.execute(
                """INSERT INTO linkedin_greeting_draft(
                     subject_id, linkedin_profile, person_name, role_text, company_name,
                     greeting_status, greeting_draft, review_reason, parse_input_hash, generated_at
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(subject_id) DO UPDATE SET
                     linkedin_profile=excluded.linkedin_profile,
                     person_name=excluded.person_name,
                     role_text=excluded.role_text,
                     company_name=excluded.company_name,
                     greeting_status=excluded.greeting_status,
                     greeting_draft=excluded.greeting_draft,
                     review_reason=excluded.review_reason,
                     parse_input_hash=excluded.parse_input_hash,
                     generated_at=excluded.generated_at""",
                (row["subject_id"], row["linkedin_profile"], row["person_name"], row["role_text"], row["company_name"], status, text, reason, row["input_hash"], now),
            )
        connection.commit()
        print({"database": str(database), "records": len(rows), "draft_ready": ready, "review": review})
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
