#!/usr/bin/env python3
"""Import ready LinkedIn greeting drafts into an explicit Workbench master."""
from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize(value: object) -> str:
    return str(value or "").strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--draft-library", required=True, type=Path, help="Exact SQLite library that contains linkedin_greeting_draft")
    parser.add_argument("--master", required=True, type=Path, help="Exact Workbench LinkedIn master SQLite path")
    args = parser.parse_args()
    draft_path, master_path = args.draft_library.resolve(), args.master.resolve()
    if not draft_path.is_file() or not master_path.is_file():
        raise SystemExit("Both the draft library and master path must exist.")
    source = sqlite3.connect(f"file:{draft_path.as_posix()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target = sqlite3.connect(master_path)
    target.row_factory = sqlite3.Row
    target.execute("PRAGMA foreign_keys = ON")
    try:
        rows = source.execute("""SELECT subject_id, linkedin_profile, person_name, role_text, company_name, greeting_draft
                                 FROM linkedin_greeting_draft WHERE greeting_status='draft_ready'
                                 ORDER BY subject_id""").fetchall()
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        target.execute("BEGIN IMMEDIATE")
        inserted = updated = 0
        for row in rows:
            subject_id = normalize(row["subject_id"])
            name, role, company, profile, greeting = (normalize(row[key]) for key in ("person_name", "role_text", "company_name", "linkedin_profile", "greeting_draft"))
            if not subject_id or not name or not company or "linkedin.com/in/" not in profile.lower() or len(greeting) < 20:
                continue
            raw = {"name": name, "identity": role or "-", "current_company": company, "linkedin_profile": profile,
                   "greeting_note": greeting, "outreach_status": "", "contact_id": f"greeting_{digest(subject_id)[:20]}"}
            raw_json = json.dumps(raw, ensure_ascii=False, separators=(",", ":"))
            existing = target.execute("SELECT source_record_id, person_id, raw_json FROM person_source_records WHERE source_target='workbench_greeting_import' AND source_row_id=?", (subject_id,)).fetchone()
            if existing:
                previous = json.loads(existing["raw_json"])
                # Update the imported greeting/profile fields, retaining operator state.
                previous.update({key: value for key, value in raw.items() if key not in {"outreach_status", "contact_id"}})
                raw_json = json.dumps(previous, ensure_ascii=False, separators=(",", ":"))
                canonical_row = target.execute("SELECT canonical_json FROM people WHERE person_id=?", (existing["person_id"],)).fetchone()
                canonical = json.loads(canonical_row["canonical_json"] or "{}")
                canonical.update({key: value for key, value in raw.items() if key not in {"outreach_status", "contact_id"}})
                canonical_json = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
                target.execute("UPDATE people SET display_name=?,normalized_name=?,canonical_identity=?,normalized_identity=?,linkedin_profile=?,normalized_profile=?,canonical_json=?,updated_at=? WHERE person_id=?", (name, name.casefold(), role or None, role.casefold() or None, profile, profile.casefold(), canonical_json, now, existing["person_id"]))
                target.execute("UPDATE person_source_records SET raw_name=?,raw_identity=?,raw_linkedin_profile=?,normalized_name=?,normalized_identity=?,normalized_profile=?,raw_json=?,record_sha256=?,created_at=? WHERE source_record_id=?", (name, role or None, profile, name.casefold(), role.casefold() or None, profile.casefold(), raw_json, digest(raw_json), now, existing["source_record_id"]))
                updated += 1
                continue
            token = digest(subject_id)[:20]; person_id = f"greeting_person_{token}"; source_id = f"greeting_record_{token}"
            target.execute("INSERT INTO people VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (person_id, name, name.casefold(), role or None, role.casefold() or None, None, None, profile, profile.casefold(), source_id, 1, 1, raw_json, now, now))
            line_no = int(token[:12], 16)
            target.execute("INSERT INTO person_source_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (source_id, person_id, "workbench_greeting_import", line_no, "workbench_greeting_import", subject_id, name, role or None, None, profile, name.casefold(), role.casefold() or None, None, profile.casefold(), raw_json, digest(raw_json), now))
            inserted += 1
        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or target.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("master validation failed")
        target.commit()
    except BaseException:
        target.rollback()
        raise
    finally:
        source.close(); target.close()
    print({"draft_library": str(draft_path), "master": str(master_path), "ready": len(rows), "inserted": inserted, "updated": updated})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
