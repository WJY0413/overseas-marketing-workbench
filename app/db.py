from collections.abc import Generator
import logging
from pathlib import Path
import sqlite3

from sqlalchemy import Engine, event, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.reports_dir.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False, "timeout": 30} if settings.resolved_database_url.startswith("sqlite") else {}
engine = create_engine(settings.resolved_database_url, connect_args=connect_args, pool_pre_ping=True)

logger = logging.getLogger(__name__)
COMPACT_STORAGE_VERSION_KEY = "compact_template_storage_version"
COMPACT_STORAGE_VERSION = "2"
COMPACT_STORAGE_PENDING_VACUUM = "2-pending-vacuum"
ACTIVE_DRAFT_STATUSES = ("pending_review", "approved", "queued", "sending", "send_unknown")
RAW_BDDB_SHADOW_MODE = "json_authoritative_sqlite_shadow"


@event.listens_for(engine, "connect")
def _configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
    """Make lock waits explicit instead of failing after SQLite's implicit 5 s."""
    if not settings.resolved_database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA busy_timeout = 30000")
    finally:
        cursor.close()


def is_unified_database(target_engine: Engine | None = None) -> bool:
    bind = target_engine or engine
    return "unified_schema_version" in set(inspect(bind).get_table_names())


def create_db_and_tables(
    target_engine: Engine | None = None,
    *,
    run_storage_migrations: bool = True,
) -> None:
    bind = target_engine or engine
    _refuse_raw_bddb_shadow(bind)
    _configure_sqlite_file_runtime(bind)
    SQLModel.metadata.create_all(bind)
    _migrate_sqlite_columns(bind)
    if run_storage_migrations:
        _migrate_compact_template_storage(bind)


def _configure_sqlite_file_runtime(target_engine: Engine) -> None:
    """Use WAL only for file-backed Workbench databases; :memory: tests stay isolated."""
    if target_engine.url.get_backend_name() != "sqlite":
        return
    database_value = target_engine.url.database
    if not database_value or database_value == ":memory:":
        return
    with target_engine.begin() as connection:
        connection.execute(text("PRAGMA busy_timeout = 30000"))
        connection.execute(text("PRAGMA journal_mode = WAL"))


def _refuse_raw_bddb_shadow(target_engine: Engine) -> None:
    """Never let Workbench promote a replaceable JSON shadow in place."""
    if target_engine.url.get_backend_name() != "sqlite":
        return
    database_value = target_engine.url.database
    if not database_value or database_value == ":memory:":
        return
    database_path = Path(database_value)
    if not database_path.is_file():
        return
    connection = sqlite3.connect(f"file:{database_path.resolve().as_posix()}?mode=ro", uri=True)
    try:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "mirror_metadata" not in tables or "unified_schema_version" in tables:
            return
        mode_row = connection.execute(
            "SELECT value FROM mirror_metadata WHERE key='mode'"
        ).fetchone()
        if mode_row and mode_row[0] == RAW_BDDB_SHADOW_MODE:
            raise RuntimeError(
                "Refusing to attach Workbench directly to the replaceable BDdb JSON shadow. "
                "Build and validate a unified SQL database first."
            )
    finally:
        connection.close()


def _migrate_sqlite_columns(target_engine: Engine) -> None:
    if target_engine.url.get_backend_name() != "sqlite":
        return
    inspector = inspect(target_engine)
    tables = set(inspector.get_table_names())
    with target_engine.begin() as conn:
        if "companies" in tables:
            columns = {column["name"] for column in inspector.get_columns("companies")}
            if "customer_library" not in columns:
                conn.execute(text("ALTER TABLE companies ADD COLUMN customer_library TEXT NOT NULL DEFAULT 'unclassified'"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_companies_customer_library ON companies (customer_library)"))
            if "primary_business_region" not in columns:
                conn.execute(text("ALTER TABLE companies ADD COLUMN primary_business_region TEXT"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS idx_companies_primary_business_region ON companies (primary_business_region)"))
        if "email_templates" in tables:
            columns = {column["name"] for column in inspector.get_columns("email_templates")}
            if "cc_enabled" not in columns:
                conn.execute(text("ALTER TABLE email_templates ADD COLUMN cc_enabled BOOLEAN DEFAULT 0 NOT NULL"))
            if "cc_emails" not in columns:
                conn.execute(text("ALTER TABLE email_templates ADD COLUMN cc_emails TEXT"))
            if "target_countries" not in columns:
                conn.execute(text("ALTER TABLE email_templates ADD COLUMN target_countries TEXT NOT NULL DEFAULT '通用'"))
            if "target_types" not in columns:
                conn.execute(text("ALTER TABLE email_templates ADD COLUMN target_types TEXT NOT NULL DEFAULT '通用'"))
            if "product_tags" not in columns:
                conn.execute(text("ALTER TABLE email_templates ADD COLUMN product_tags TEXT NOT NULL DEFAULT '通用'"))
        if "email_drafts" in tables:
            columns = {column["name"] for column in inspector.get_columns("email_drafts")}
            indexes = {index["name"] for index in inspector.get_indexes("email_drafts")}
            if "cc_emails" not in columns:
                conn.execute(text("ALTER TABLE email_drafts ADD COLUMN cc_emails TEXT"))
            if "attachment_paths" not in columns:
                conn.execute(text("ALTER TABLE email_drafts ADD COLUMN attachment_paths TEXT"))
            if "signature_template_id" not in columns:
                conn.execute(text("ALTER TABLE email_drafts ADD COLUMN signature_template_id INTEGER REFERENCES email_templates(template_id)"))
            if "idx_email_drafts_signature_template" not in indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_email_drafts_signature_template ON email_drafts (signature_template_id)"))
        if "activity_records" in tables:
            columns = {column["name"] for column in inspector.get_columns("activity_records")}
            indexes = {index["name"] for index in inspector.get_indexes("activity_records")}
            if "attachment_paths" not in columns:
                conn.execute(text("ALTER TABLE activity_records ADD COLUMN attachment_paths TEXT"))
            if "template_id" not in columns:
                conn.execute(text("ALTER TABLE activity_records ADD COLUMN template_id INTEGER REFERENCES email_templates(template_id)"))
            if "signature_template_id" not in columns:
                conn.execute(text("ALTER TABLE activity_records ADD COLUMN signature_template_id INTEGER REFERENCES email_templates(template_id)"))
            if "idx_activity_template" not in indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_activity_template ON activity_records (template_id)"))
            if "idx_activity_signature_template" not in indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_activity_signature_template ON activity_records (signature_template_id)"))
        if "draft_prep_items" in tables:
            columns = {column["name"] for column in inspector.get_columns("draft_prep_items")}
            indexes = {index["name"] for index in inspector.get_indexes("draft_prep_items")}
            if "contact_id" in columns and "idx_draft_prep_contact" not in indexes:
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS idx_draft_prep_contact ON draft_prep_items (contact_id)"))
        if "sender_accounts" in tables:
            columns = {column["name"] for column in inspector.get_columns("sender_accounts")}
            if "smtp_password_encrypted" not in columns:
                conn.execute(text("ALTER TABLE sender_accounts ADD COLUMN smtp_password_encrypted TEXT"))
            if "send_timezone" not in columns:
                conn.execute(text("ALTER TABLE sender_accounts ADD COLUMN send_timezone TEXT"))
            if "send_windows_json" not in columns:
                conn.execute(text("ALTER TABLE sender_accounts ADD COLUMN send_windows_json TEXT"))
        if "contacts" in tables:
            columns = {column["name"] for column in inspector.get_columns("contacts")}
            indexes = {index["name"] for index in inspector.get_indexes("contacts")}
            if "priority_contact_rank" not in columns:
                conn.execute(text("ALTER TABLE contacts ADD COLUMN priority_contact_rank INTEGER"))
            if "idx_contacts_priority_rank" not in indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_contacts_priority_rank ON contacts (priority_contact_rank)"))
        if "contact_routes" in tables:
            indexes = {index["name"] for index in inspector.get_indexes("contact_routes")}
            if "idx_contact_routes_contact_type" not in indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS idx_contact_routes_contact_type ON contact_routes (contact_id, route_type)"))
        if "master_sync_runs" in tables:
            columns = {column["name"] for column in inspector.get_columns("master_sync_runs")}
            if "quarantine_company_count" not in columns:
                conn.execute(text("ALTER TABLE master_sync_runs ADD COLUMN quarantine_company_count INTEGER DEFAULT 0 NOT NULL"))
            if "quarantine_email_count" not in columns:
                conn.execute(text("ALTER TABLE master_sync_runs ADD COLUMN quarantine_email_count INTEGER DEFAULT 0 NOT NULL"))
            if "fatal_conflict_count" not in columns:
                conn.execute(text("ALTER TABLE master_sync_runs ADD COLUMN fatal_conflict_count INTEGER DEFAULT 0 NOT NULL"))


def _set_compact_storage_version(connection: sqlite3.Connection, value: str) -> None:
    connection.execute(
        """
        INSERT INTO app_settings ("key", "value", updated_at)
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
        required_tables = {"app_settings", "email_drafts", "activity_records"}
        if not required_tables.issubset(tables):
            return
        marker_row = connection.execute(
            'SELECT "value" FROM app_settings WHERE "key" = ?',
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
                    UPDATE activity_records
                    SET template_id = (
                        SELECT email_drafts.template_id
                        FROM email_drafts
                        WHERE email_drafts.draft_id = activity_records.draft_id
                    )
                    WHERE template_id IS NULL
                    """
                )
                connection.execute(
                    """
                    UPDATE activity_records
                    SET signature_template_id = (
                        SELECT email_drafts.signature_template_id
                        FROM email_drafts
                        WHERE email_drafts.draft_id = activity_records.draft_id
                    )
                    WHERE signature_template_id IS NULL
                    """
                )
                rows_cleared += connection.execute(
                    """
                    UPDATE activity_records
                    SET body_html = '', body_text = NULL
                    WHERE LENGTH(COALESCE(body_html, '')) > 0
                       OR LENGTH(COALESCE(body_text, '')) > 0
                    """
                ).rowcount
                placeholders = ",".join("?" for _ in ACTIVE_DRAFT_STATUSES)
                rows_cleared += connection.execute(
                    f"""
                    UPDATE email_drafts
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
