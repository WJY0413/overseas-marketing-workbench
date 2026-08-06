#!/usr/bin/env python3
"""Inspect and narrowly manage Workbench email drafts.

Default behavior is read-only. Writes require --apply and an explicit action.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path


DEFAULT_DB = Path(r"<detected-workbench-root>\data\workbench.db")
WRITABLE_TARGET_STATUSES = {"draft", "approved", "queued", "cancelled"}
PROTECTED_STATUSES = {"sent", "sending"}


def build_where(args: argparse.Namespace) -> tuple[str, list[object]]:
    clauses: list[str] = []
    params: list[object] = []
    if args.ids:
        ids = [int(x.strip()) for x in args.ids.split(",") if x.strip()]
        clauses.append("d.id in (%s)" % ",".join("?" for _ in ids))
        params.extend(ids)
    if args.status:
        clauses.append("d.status = ?")
        params.append(args.status)
    if args.follow_up_step is not None:
        clauses.append("d.follow_up_step = ?")
        params.append(args.follow_up_step)
    if args.template_id is not None:
        clauses.append("d.template_id = ?")
        params.append(args.template_id)
    where = " and ".join(clauses) if clauses else "1=1"
    return where, params


def print_matches(cur: sqlite3.Cursor, where: str, params: list[object], limit: int) -> None:
    total = cur.execute(f"select count(*) from emaildraft d where {where}", params).fetchone()[0]
    print(f"matched={total}")
    print("id\tstatus\tstep\ttemplate\tsender\trecipient\tcc\tsubject")
    for row in cur.execute(
        f"""
        select d.id, d.status, d.follow_up_step, d.template_id, d.sender_account_id,
               c.email as recipient_email, d.cc_emails, d.subject
        from emaildraft d
        left join contact c on c.id = d.contact_id
        where {where}
        order by d.id desc
        limit ?
        """,
        [*params, limit],
    ):
        print("\t".join(str(x or "") for x in row))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Workbench SQLite DB path")
    parser.add_argument("--action", choices=["inspect", "set-status", "delete"], default="inspect")
    parser.add_argument("--ids", help="Comma-separated draft ids")
    parser.add_argument("--status", help="Filter by current draft status")
    parser.add_argument("--follow-up-step", type=int, help="Filter by follow_up_step")
    parser.add_argument("--template-id", type=int, help="Filter by template_id")
    parser.add_argument("--to-status", choices=sorted(WRITABLE_TARGET_STATUSES), help="Target status for set-status")
    parser.add_argument("--limit", type=int, default=20, help="Preview row limit")
    parser.add_argument("--apply", action="store_true", help="Actually write changes")
    args = parser.parse_args()

    db = Path(args.db)
    if not db.exists():
        raise SystemExit(f"DB not found: {db}")

    con = sqlite3.connect(db)
    cur = con.cursor()
    where, params = build_where(args)

    print(f"db={db}")
    paused = cur.execute("select value from appsetting where key='queue_paused'").fetchone()
    print(f"queue_paused={paused[0] if paused else None}")
    print_matches(cur, where, params, args.limit)

    if args.action == "inspect":
        con.close()
        return 0

    protected = cur.execute(
        f"select status, count(*) from emaildraft d where {where} group by status", params
    ).fetchall()
    protected_hit = [status for status, _ in protected if status in PROTECTED_STATUSES]
    if protected_hit:
        raise SystemExit(f"Refusing to modify protected statuses: {', '.join(protected_hit)}")

    if args.action == "set-status":
        if not args.to_status:
            raise SystemExit("--to-status is required for set-status")
        sql = f"update emaildraft as d set status=?, updated_at=datetime('now') where {where}"
        write_params = [args.to_status, *params]
    elif args.action == "delete":
        sql = f"delete from emaildraft where id in (select d.id from emaildraft d where {where})"
        write_params = params
    else:
        raise SystemExit(f"Unsupported action: {args.action}")

    if not args.apply:
        print(f"dry_run=true action={args.action}; add --apply to write")
        con.close()
        return 0

    cur.execute(sql, write_params)
    print(f"changed={cur.rowcount}")
    con.commit()
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
