from collections.abc import Generator
import logging
from pathlib import Path
import sqlite3

from sqlalchemy import Engine, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.reports_dir.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.resolved_database_url.startswith("sqlite") else {}
engine = create_engine(settings.resolved_database_url, connect_args=connect_args, pool_pre_ping=True)

logger = logging.getLogger(__name__)
COMPACT_STORAGE_VERSION_KEY = "compact_template_storage_version"
COMPACT_STORAGE_VERSION = "2"
COMPACT_STORAGE_PENDING_VACUUM = "2-pending-vacuum"
ACTIVE_DRAFT_STATUSES = ("pending_review", "approved", "queued")


def create_db_and_tables(
    target_engine: Engine | None = None,
    *,
    run_storage_migrations: bool = True,
) -> None:
    bind = target_engine or engine
    SQLModel.metadata.create_all(bind)
    _migrate_sqlite_columns(bind)
    if run_storage_migrations:
        _migrate_compact_template_storage(bind)


def _migrate_sqlite_columns(target_engine: Engine) -> None:
    if target_engine.url.get_backend_name() != "sqlite":
        return
    inspector = inspect(target_engine)
    tables = set(inspector.get_table_names())
    with target_engine.begin() as conn:
        if "company" in tables:
            columns = {column["name"] for column in inspector.get_columns("company")}
            company_indexes = {index["name"]: index for index in inspector.get_indexes("company")}
            if "region" not in columns:
                conn.execute(text("ALTER TABLE company ADD COLUMN region TEXT"))
            if "ix_company_region" not in company_indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_company_region ON company (region)"))
            name_index = company_indexes.get("ix_company_name")
            if name_index and name_index.get("unique"):
                conn.execute(text("DROP INDEX ix_company_name"))
                conn.execute(text("CREATE INDEX ix_company_name ON company (name)"))
        if "emailtemplate" in tables:
            columns = {column["name"] for column in inspector.get_columns("emailtemplate")}
            if "cc_enabled" not in columns:
                conn.execute(text("ALTER TABLE emailtemplate ADD COLUMN cc_enabled BOOLEAN DEFAULT 0 NOT NULL"))
            if "cc_emails" not in columns:
                conn.execute(text("ALTER TABLE emailtemplate ADD COLUMN cc_emails TEXT"))
        if "emaildraft" in tables:
            columns = {column["name"] for column in inspector.get_columns("emaildraft")}
            indexes = {index["name"] for index in inspector.get_indexes("emaildraft")}
            if "cc_emails" not in columns:
                conn.execute(text("ALTER TABLE emaildraft ADD COLUMN cc_emails TEXT"))
            if "attachment_paths" not in columns:
                conn.execute(text("ALTER TABLE emaildraft ADD COLUMN attachment_paths TEXT"))
            if "signature_template_id" not in columns:
                conn.execute(text("ALTER TABLE emaildraft ADD COLUMN signature_template_id INTEGER REFERENCES emailtemplate(id)"))
            if "ix_emaildraft_signature_template_id" not in indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_emaildraft_signature_template_id ON emaildraft (signature_template_id)"))
        if "sendrecord" in tables:
            columns = {column["name"] for column in inspector.get_columns("sendrecord")}
            indexes = {index["name"] for index in inspector.get_indexes("sendrecord")}
            if "attachment_paths" not in columns:
                conn.execute(text("ALTER TABLE sendrecord ADD COLUMN attachment_paths TEXT"))
            if "template_id" not in columns:
                conn.execute(text("ALTER TABLE sendrecord ADD COLUMN template_id INTEGER REFERENCES emailtemplate(id)"))
            if "signature_template_id" not in columns:
                conn.execute(text("ALTER TABLE sendrecord ADD COLUMN signature_template_id INTEGER REFERENCES emailtemplate(id)"))
            if "ix_sendrecord_template_id" not in indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sendrecord_template_id ON sendrecord (template_id)"))
            if "ix_sendrecord_signature_template_id" not in indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_sendrecord_signature_template_id ON sendrecord (signature_template_id)"))
        if "draftprepitem" in tables:
            columns = {column["name"] for column in inspector.get_columns("draftprepitem")}
            indexes = {index["name"] for index in inspector.get_indexes("draftprepitem")}
            if "contact_id" in columns and "ix_draftprepitem_contact_id" not in indexes:
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_draftprepitem_contact_id ON draftprepitem (contact_id)"))
        if "senderaccount" in tables:
            columns = {column["name"] for column in inspector.get_columns("senderaccount")}
            if "smtp_password_encrypted" not in columns:
                conn.execute(text("ALTER TABLE senderaccount ADD COLUMN smtp_password_encrypted TEXT"))
            if "send_timezone" not in columns:
                conn.execute(text("ALTER TABLE senderaccount ADD COLUMN send_timezone TEXT"))
            if "send_windows_json" not in columns:
                conn.execute(text("ALTER TABLE senderaccount ADD COLUMN send_windows_json TEXT"))
        if "contact" in tables:
            columns = {column["name"] for column in inspector.get_columns("contact")}
            indexes = {index["name"] for index in inspector.get_indexes("contact")}
            if "priority_contact_rank" not in columns:
                conn.execute(text("ALTER TABLE contact ADD COLUMN priority_contact_rank INTEGER"))
            if "ix_contact_priority_contact_rank" not in indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_contact_priority_contact_rank ON contact (priority_contact_rank)"))
        if "contactroute" in tables:
            indexes = {index["name"] for index in inspector.get_indexes("contactroute")}
            if "ix_contactroute_contact_type" not in indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_contactroute_contact_type ON contactroute (contact_id, route_type)"))
        if "bdmastersyncrun" in tables:
            columns = {column["name"] for column in inspector.get_columns("bdmastersyncrun")}
            if "quarantine_company_count" not in columns:
                conn.execute(text("ALTER TABLE bdmastersyncrun ADD COLUMN quarantine_company_count INTEGER DEFAULT 0 NOT NULL"))
            if "quarantine_email_count" not in columns:
                conn.execute(text("ALTER TABLE bdmastersyncrun ADD COLUMN quarantine_email_count INTEGER DEFAULT 0 NOT NULL"))
            if "fatal_conflict_count" not in columns:
                conn.execute(text("ALTER TABLE bdmastersyncrun ADD COLUMN fatal_conflict_count INTEGER DEFAULT 0 NOT NULL"))


def _set_compact_storage_version(connection: sqlite3.Connection, value: str) -> None:
    connection.execute(
        """
        INSERT INTO appsetting ("key", "value", updated_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT("key") DO UPDATE
        SET "value" = excluded."value", updated_at = CURRENT_TIMESTAMP
        """,
        (COMPACT_STORAGE_VERSION_KEY, value),
    )


def _migrate_compact_template_storage(target_engine: Engine) -> None:
    if target_engine.url.get_backend_name() != "sqlite":
        return
    database_value = target_engine.url.database
    if not database_value or database_value == ":memory:":
        return
    database_path = Path(database_value)
    if not database_path.exists():
        return

    connection = sqlite3.connect(database_path, timeout=30)
    connection.execute("PRAGMA busy_timeout = 30000")
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        required_tables = {"appsetting", "emaildraft", "sendrecord"}
        if not required_tables.issubset(tables):
            return
        marker_row = connection.execute(
            'SELECT "value" FROM appsetting WHERE "key" = ?',
            (COMPACT_STORAGE_VERSION_KEY,),
        ).fetchone()
        marker = marker_row[0] if marker_row else None
        if marker == COMPACT_STORAGE_VERSION:
            return
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            logger.error(
                "Skipped compact storage migration because SQLite integrity_check returned %s.",
                integrity,
            )
            return

        rows_cleared = 0
        if marker != COMPACT_STORAGE_PENDING_VACUUM:
            connection.execute("BEGIN IMMEDIATE")
            try:
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
                rows_cleared += connection.execute(
                    """
                    UPDATE sendrecord
                    SET body_html = '', body_text = NULL
                    WHERE LENGTH(COALESCE(body_html, '')) > 0
                       OR LENGTH(COALESCE(body_text, '')) > 0
                    """
                ).rowcount
                placeholders = ",".join("?" for _ in ACTIVE_DRAFT_STATUSES)
                rows_cleared += connection.execute(
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
                rows_cleared += connection.execute(
                    f"""
                    UPDATE emaildraft
                    SET template_snapshot = NULL
                    WHERE status IN ({placeholders})
                      AND LENGTH(COALESCE(template_snapshot, '')) > 0
                    """,
                    ACTIVE_DRAFT_STATUSES,
                ).rowcount
                _set_compact_storage_version(
                    connection,
                    COMPACT_STORAGE_PENDING_VACUUM
                    if rows_cleared
                    else COMPACT_STORAGE_VERSION,
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

        if marker == COMPACT_STORAGE_PENDING_VACUUM or rows_cleared:
            try:
                connection.execute("VACUUM")
                connection.execute("PRAGMA optimize")
                post_integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
                if post_integrity != "ok":
                    raise RuntimeError(
                        f"post-migration integrity_check failed: {post_integrity}"
                    )
                _set_compact_storage_version(connection, COMPACT_STORAGE_VERSION)
                connection.commit()
                logger.info(
                    "Compact storage migration completed for %s; rows cleared: %s.",
                    database_path,
                    rows_cleared,
                )
            except (RuntimeError, sqlite3.DatabaseError):
                logger.exception(
                    "Compact storage cleanup was applied, but VACUUM could not finish. "
                    "The app will retry VACUUM on the next startup."
                )
    finally:
        connection.close()


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
