#!/usr/bin/env python3
"""Pause the Workbench queue with a narrow SQLite update."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


DEFAULT_DB = Path(r"<detected-workbench-root>\data\workbench.db")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Workbench SQLite DB path")
    parser.add_argument("--dry-run", action="store_true", help="Show state without writing")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"DB not found: {db}")

    con = sqlite3.connect(db)
    cur = con.cursor()
    before = cur.execute("select value from appsetting where key='queue_paused'").fetchone()
    print(f"before_queue_paused={before[0] if before else None}")

    if not args.dry_run:
        cur.execute(
            "update appsetting set value='true', updated_at=datetime('now') where key='queue_paused'"
        )
        if cur.rowcount == 0:
            cur.execute(
                """
                insert into appsetting(key, value, updated_at)
                values('queue_paused', 'true', datetime('now'))
                """
            )
        con.commit()

    after = cur.execute("select value from appsetting where key='queue_paused'").fetchone()
    print(f"after_queue_paused={after[0] if after else None}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
