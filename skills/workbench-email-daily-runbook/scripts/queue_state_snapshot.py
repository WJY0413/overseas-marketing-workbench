#!/usr/bin/env python3
"""Read-only Workbench queue and draft state snapshot."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


DEFAULT_DB = Path(r"D:\BD_Email_Workbench\production\data\workbench.db")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Workbench SQLite DB path")
    parser.add_argument("--limit", type=int, default=10, help="Recent draft sample limit")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"DB not found: {db}")

    con = sqlite3.connect(db)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    paused = cur.execute("select value from appsetting where key='queue_paused'").fetchone()
    print(f"db={db}")
    print(f"queue_paused={paused['value'] if paused else None}")

    print("\ndraft_status_counts")
    for row in cur.execute("select status, count(*) as n from emaildraft group by status order by status"):
        print(f"{row['status']}\t{row['n']}")

    latest_send = cur.execute(
        "select count(*) as n, max(sent_at) as latest_sent_at from sendrecord"
    ).fetchone()
    print(f"\nsendrecord_total={latest_send['n']}")
    print(f"latest_sent_at={latest_send['latest_sent_at']}")

    print("\nrecent_drafts")
    for row in cur.execute(
        """
        select d.id, d.status, d.follow_up_step, d.sender_account_id,
               c.email as recipient_email, d.cc_emails, d.subject, d.created_at, d.scheduled_at
        from emaildraft d
        left join contact c on c.id = d.contact_id
        order by d.id desc
        limit ?
        """,
        (args.limit,),
    ):
        print(
            "\t".join(
                str(row[k] or "")
                for k in [
                    "id",
                    "status",
                    "follow_up_step",
                    "sender_account_id",
                    "recipient_email",
                    "cc_emails",
                    "subject",
                    "created_at",
                    "scheduled_at",
                ]
            )
        )

    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
