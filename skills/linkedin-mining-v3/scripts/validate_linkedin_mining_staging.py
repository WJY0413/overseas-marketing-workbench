#!/usr/bin/env python3
"""Validate the non-promoting LinkedIn mining staging database."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    args = parser.parse_args()
    if not args.database.exists():
        print(f"staging validation failed: database does not exist: {args.database}", file=sys.stderr)
        return 2
    connection = sqlite3.connect(f"file:{args.database.resolve().as_posix()}?mode=ro", uri=True)
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        invalid_profiles = connection.execute(
            "SELECT count(*) FROM candidate WHERE canonical_profile_url NOT LIKE 'https://www.linkedin.com/in/%'"
        ).fetchone()[0]
        invalid_labels = connection.execute(
            """SELECT count(*) FROM candidate_label AS tag
               LEFT JOIN allowed_label AS allowed
                 ON allowed.label_namespace = tag.label_namespace AND allowed.label_value = tag.label_value
               WHERE allowed.label_namespace IS NULL"""
        ).fetchone()[0]
        runs = connection.execute("SELECT count(*) FROM capture_run").fetchone()[0]
        pages = connection.execute("SELECT count(*) FROM capture_page").fetchone()[0]
        candidates = connection.execute("SELECT count(*) FROM candidate").fetchone()[0]
        direct_links = connection.execute("SELECT count(*) FROM profile_ledger").fetchone()[0]
        promoted = connection.execute("SELECT count(*) FROM candidate WHERE promotion_state = 'promoted'").fetchone()[0]
    finally:
        connection.close()
    result = {
        "database": str(args.database.resolve()),
        "integrity": integrity,
        "foreign_key_violations": len(foreign_keys),
        "invalid_profiles": invalid_profiles,
        "invalid_labels": invalid_labels,
        "runs": runs,
        "pages": pages,
        "candidates": candidates,
        "direct_links": direct_links,
        "promoted_candidates": promoted,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if integrity == "ok" and not foreign_keys and not invalid_profiles and not invalid_labels and not promoted else 2


if __name__ == "__main__":
    raise SystemExit(main())
