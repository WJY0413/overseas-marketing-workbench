import argparse
import json
import sqlite3
from pathlib import Path


ACTIVE_DRAFT_STATUSES = ("pending_review", "approved", "queued")
COMPACT_STORAGE_VERSION_KEY = "compact_template_storage_version"
COMPACT_STORAGE_VERSION = "2"


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _payload_chars(connection: sqlite3.Connection) -> dict[str, int]:
    return {
        "emaildraft_body_html": connection.execute(
            "SELECT COALESCE(SUM(LENGTH(COALESCE(body_html, ''))), 0) FROM emaildraft"
        ).fetchone()[0],
        "emaildraft_template_snapshot": connection.execute(
            "SELECT COALESCE(SUM(LENGTH(COALESCE(template_snapshot, ''))), 0) FROM emaildraft"
        ).fetchone()[0],
        "sendrecord_body_html": connection.execute(
            "SELECT COALESCE(SUM(LENGTH(COALESCE(body_html, ''))), 0) FROM sendrecord"
        ).fetchone()[0],
    }


def compact_database(database_path: Path, *, vacuum: bool) -> dict:
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA busy_timeout = 5000")
    before_integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if before_integrity != "ok":
        raise RuntimeError(f"pre-migration integrity_check failed: {before_integrity}")

    before_bytes = database_path.stat().st_size
    before_payload = _payload_chars(connection)
    sendrecord_columns = _column_names(connection, "sendrecord")
    emaildraft_columns = _column_names(connection, "emaildraft")

    connection.execute("BEGIN IMMEDIATE")
    try:
        if "template_id" not in sendrecord_columns:
            connection.execute(
                "ALTER TABLE sendrecord ADD COLUMN template_id INTEGER REFERENCES emailtemplate(id)"
            )
        if "signature_template_id" not in sendrecord_columns:
            connection.execute(
                "ALTER TABLE sendrecord ADD COLUMN signature_template_id INTEGER REFERENCES emailtemplate(id)"
            )
        if "signature_template_id" not in emaildraft_columns:
            connection.execute(
                "ALTER TABLE emaildraft ADD COLUMN signature_template_id INTEGER REFERENCES emailtemplate(id)"
            )

        connection.execute(
            """
            UPDATE sendrecord
            SET template_id = (
                SELECT emaildraft.template_id
                FROM emaildraft
                WHERE emaildraft.id = sendrecord.draft_id
            )
            WHERE template_id IS NULL
            """
        )
        connection.execute(
            """
            UPDATE sendrecord
            SET signature_template_id = (
                SELECT emaildraft.signature_template_id
                FROM emaildraft
                WHERE emaildraft.id = sendrecord.draft_id
            )
            WHERE signature_template_id IS NULL
            """
        )
        sendrecord_rows_cleared = connection.execute(
            """
            UPDATE sendrecord
            SET body_html = '', body_text = NULL
            WHERE LENGTH(COALESCE(body_html, '')) > 0
               OR LENGTH(COALESCE(body_text, '')) > 0
            """
        ).rowcount
        placeholders = ",".join("?" for _ in ACTIVE_DRAFT_STATUSES)
        draft_rows_cleared = connection.execute(
            f"""
            UPDATE emaildraft
            SET body_html = '', body_text = NULL, template_snapshot = NULL
            WHERE status NOT IN ({placeholders})
              AND (
                    LENGTH(COALESCE(body_html, '')) > 0
                 OR LENGTH(COALESCE(body_text, '')) > 0
                 OR LENGTH(COALESCE(template_snapshot, '')) > 0
              )
            """,
            ACTIVE_DRAFT_STATUSES,
        ).rowcount
        active_snapshots_cleared = connection.execute(
            f"""
            UPDATE emaildraft
            SET template_snapshot = NULL
            WHERE status IN ({placeholders})
              AND LENGTH(COALESCE(template_snapshot, '')) > 0
            """,
            ACTIVE_DRAFT_STATUSES,
        ).rowcount
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_sendrecord_template_id ON sendrecord (template_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_sendrecord_signature_template_id ON sendrecord (signature_template_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS ix_emaildraft_signature_template_id ON emaildraft (signature_template_id)"
        )
        connection.commit()
    except Exception:
        connection.rollback()
        raise

    if vacuum:
        connection.execute("VACUUM")
    connection.execute("PRAGMA optimize")
    after_integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    if after_integrity != "ok":
        raise RuntimeError(f"post-migration integrity_check failed: {after_integrity}")
    after_payload = _payload_chars(connection)
    template_backfill = connection.execute(
        """
        SELECT
            COUNT(*),
            SUM(CASE WHEN template_id IS NOT NULL THEN 1 ELSE 0 END)
        FROM sendrecord
        """
    ).fetchone()
    connection.execute(
        """
        INSERT INTO appsetting ("key", "value", updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT("key") DO UPDATE
        SET "value" = excluded."value", updated_at = CURRENT_TIMESTAMP
        """,
        (COMPACT_STORAGE_VERSION_KEY, COMPACT_STORAGE_VERSION),
    )
    connection.commit()
    connection.close()

    return {
        "database": str(database_path),
        "before_bytes": before_bytes,
        "after_bytes": database_path.stat().st_size,
        "before_payload_chars": before_payload,
        "after_payload_chars": after_payload,
        "sendrecord_rows_cleared": sendrecord_rows_cleared,
        "draft_rows_cleared": draft_rows_cleared,
        "active_snapshots_cleared": active_snapshots_cleared,
        "sendrecord_total": template_backfill[0],
        "sendrecord_with_template_id": template_backfill[1],
        "integrity_check": after_integrity,
        "vacuum": vacuum,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--no-vacuum", action="store_true")
    args = parser.parse_args()

    database_path = args.database.resolve()
    if not database_path.exists():
        raise SystemExit(f"database not found: {database_path}")
    if not args.apply:
        print(
            json.dumps(
                {
                    "database": str(database_path),
                    "bytes": database_path.stat().st_size,
                    "status": "dry-run only; pass --apply to migrate",
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return
    result = compact_database(database_path, vacuum=not args.no_vacuum)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
