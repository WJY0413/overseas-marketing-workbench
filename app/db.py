from collections.abc import Generator
from pathlib import Path

from sqlalchemy import Engine, inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.reports_dir.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.resolved_database_url.startswith("sqlite") else {}
engine = create_engine(settings.resolved_database_url, connect_args=connect_args, pool_pre_ping=True)


def create_db_and_tables(
    target_engine: Engine | None = None,
    *,
    run_storage_migrations: bool = True,
) -> None:
    # run_storage_migrations is accepted for the standalone sync auditor. The
    # production 0.4.17 branch has no compact-storage migration to run.
    bind = target_engine or engine
    SQLModel.metadata.create_all(bind)
    _migrate_sqlite_columns(bind)


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
        if "suppression" in tables:
            columns = {column["name"] for column in inspector.get_columns("suppression")}
            indexes = {index["name"] for index in inspector.get_indexes("suppression")}
            if "expires_at" not in columns:
                conn.execute(text("ALTER TABLE suppression ADD COLUMN expires_at DATETIME"))
            if "ix_suppression_expires_at" not in indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_suppression_expires_at ON suppression (expires_at)"))
        if "bdmastersyncrun" in tables:
            columns = {column["name"] for column in inspector.get_columns("bdmastersyncrun")}
            if "quarantine_company_count" not in columns:
                conn.execute(text("ALTER TABLE bdmastersyncrun ADD COLUMN quarantine_company_count INTEGER DEFAULT 0 NOT NULL"))
            if "quarantine_email_count" not in columns:
                conn.execute(text("ALTER TABLE bdmastersyncrun ADD COLUMN quarantine_email_count INTEGER DEFAULT 0 NOT NULL"))
            if "fatal_conflict_count" not in columns:
                conn.execute(text("ALTER TABLE bdmastersyncrun ADD COLUMN fatal_conflict_count INTEGER DEFAULT 0 NOT NULL"))


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
