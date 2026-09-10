"""Build and read the self-contained, read-only LinkedIn test library."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path


CONNECTED_STATUS = "connection_attempted"
LIBRARY_TABLE = "linkedin_test_library_records"
TEST_GREETING_WARNING = "【测试样例】此为内置 LinkedIn 测试数据，仅用于演示；请勿直接复制、发送或用于真实客户沟通。"


def _source_connection(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise ValueError(f"LinkedIn source library not found: {path}")
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _company(raw: dict, person_id: str) -> str:
    for key in ("resolved_company", "company_name", "current_company"):
        value = str(raw.get(key) or "").strip()
        if value:
            return value
    return person_id


def build_connected_company_library(source_path: Path, target_path: Path, *, company_limit: int = 100) -> dict[str, int]:
    """Copy one existing connected contact from each company into an isolated library.

    The source is opened read-only. The resulting file intentionally has no
    confirmation, generation, or mutation tables: it is a display/import seed.
    Greeting content is always replaced by a Chinese test warning.
    """
    if company_limit < 1:
        raise ValueError("company_limit must be positive")
    if source_path.resolve() == target_path.resolve():
        raise ValueError("target library must not replace the source library")

    source = _source_connection(source_path)
    selected: list[dict[str, str]] = []
    companies: set[str] = set()
    try:
        rows = source.execute(
            """SELECT source_record_id, person_id, raw_name, raw_linkedin_profile, raw_json
               FROM person_source_records
               WHERE lower(coalesce(json_extract(raw_json, '$.outreach_status'), '')) = ?
               ORDER BY source_record_id""",
            (CONNECTED_STATUS,),
        )
        for row in rows:
            raw = json.loads(row["raw_json"])
            company = _company(raw, str(row["person_id"]))
            company_key = company.casefold()
            if company_key in companies:
                continue
            profile = str(raw.get("linkedin_profile") or row["raw_linkedin_profile"] or "").strip()
            if "linkedin.com/in/" not in profile.lower():
                continue
            selected.append(
                {
                    "source_record_id": str(row["source_record_id"]),
                    "person_id": str(row["person_id"]),
                    "name": str(raw.get("name") or row["raw_name"] or "-").strip() or "-",
                    "position": str(raw.get("identity") or raw.get("role_title") or raw.get("role_value") or "-").strip() or "-",
                    "company": company,
                    "linkedin_profile": profile,
                    "greeting_note": TEST_GREETING_WARNING,
                }
            )
            companies.add(company_key)
            if len(selected) == company_limit:
                break
    finally:
        source.close()

    if len(selected) != company_limit:
        raise ValueError(f"Expected {company_limit} connected companies; found {len(selected)}")

    target_path.parent.mkdir(parents=True, exist_ok=True)
    if target_path.exists():
        target_path.unlink()
    target = sqlite3.connect(target_path)
    try:
        target.execute(
            f"""CREATE TABLE {LIBRARY_TABLE} (
                source_record_id TEXT PRIMARY KEY,
                person_id TEXT NOT NULL,
                name TEXT NOT NULL,
                position TEXT NOT NULL,
                company TEXT NOT NULL,
                linkedin_profile TEXT NOT NULL,
                greeting_note TEXT NOT NULL
            )"""
        )
        target.executemany(
            f"""INSERT INTO {LIBRARY_TABLE}
                (source_record_id, person_id, name, position, company, linkedin_profile, greeting_note)
                VALUES (:source_record_id, :person_id, :name, :position, :company, :linkedin_profile, :greeting_note)""",
            selected,
        )
        target.execute("CREATE UNIQUE INDEX idx_linkedin_test_library_company ON linkedin_test_library_records(company COLLATE NOCASE)")
        target.commit()
    finally:
        target.close()
    return {"company_count": len(selected), "record_count": len(selected)}


def library_record_count(path: Path, *, query: str = "") -> int:
    if not path.is_file():
        return 0
    needle = f"%{query.strip().lower()}%"
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        return int(connection.execute(
            f"SELECT COUNT(*) FROM {LIBRARY_TABLE} WHERE lower(name || ' ' || company || ' ' || position) LIKE ?",
            (needle,),
        ).fetchone()[0])
    finally:
        connection.close()


def list_library_records(path: Path, *, query: str = "", limit: int = 10, offset: int = 0) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    if limit < 1 or offset < 0:
        raise ValueError("limit must be positive and offset must not be negative")
    needle = f"%{query.strip().lower()}%"
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            f"""SELECT source_record_id, name, position, company, linkedin_profile, greeting_note
               FROM {LIBRARY_TABLE}
               WHERE lower(name || ' ' || company || ' ' || position) LIKE ?
               ORDER BY company COLLATE NOCASE, name COLLATE NOCASE
               LIMIT ? OFFSET ?""",
            (needle, limit, offset),
        )
        return [dict(row) for row in rows]
    finally:
        connection.close()


def library_confirmed_page(path: Path, *, query: str = "", page: int = 1, page_size: int = 10) -> dict[str, object]:
    """Return the read-only built-in library as already-confirmed connections."""
    total = library_record_count(path, query=query)
    page_count = max(1, (total + page_size - 1) // page_size)
    page = min(max(1, page), page_count)
    return {
        "total": total,
        "page": page,
        "page_count": page_count,
        "records": list_library_records(path, query=query, limit=page_size, offset=(page - 1) * page_size),
    }
