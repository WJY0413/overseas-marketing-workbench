from collections.abc import Generator
from pathlib import Path

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine

from app.config import get_settings

settings = get_settings()
settings.data_dir.mkdir(parents=True, exist_ok=True)
settings.reports_dir.mkdir(parents=True, exist_ok=True)

connect_args = {"check_same_thread": False} if settings.resolved_database_url.startswith("sqlite") else {}
engine = create_engine(settings.resolved_database_url, connect_args=connect_args, pool_pre_ping=True)


def create_db_and_tables() -> None:
    SQLModel.metadata.create_all(engine)
    _migrate_sqlite_columns()


def _migrate_sqlite_columns() -> None:
    if not get_settings().resolved_database_url.startswith("sqlite"):
        return
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    with engine.begin() as conn:
        if "company" in tables:
            columns = {column["name"] for column in inspector.get_columns("company")}
            indexes = {index["name"] for index in inspector.get_indexes("company")}
            if "region" not in columns:
                conn.execute(text("ALTER TABLE company ADD COLUMN region TEXT"))
            if "ix_company_region" not in indexes:
                conn.execute(text("CREATE INDEX IF NOT EXISTS ix_company_region ON company (region)"))
        if "emailtemplate" in tables:
            columns = {column["name"] for column in inspector.get_columns("emailtemplate")}
            if "cc_enabled" not in columns:
                conn.execute(text("ALTER TABLE emailtemplate ADD COLUMN cc_enabled BOOLEAN DEFAULT 0 NOT NULL"))
            if "cc_emails" not in columns:
                conn.execute(text("ALTER TABLE emailtemplate ADD COLUMN cc_emails TEXT"))
        if "emaildraft" in tables:
            columns = {column["name"] for column in inspector.get_columns("emaildraft")}
            if "cc_emails" not in columns:
                conn.execute(text("ALTER TABLE emaildraft ADD COLUMN cc_emails TEXT"))
            if "attachment_paths" not in columns:
                conn.execute(text("ALTER TABLE emaildraft ADD COLUMN attachment_paths TEXT"))
        if "sendrecord" in tables:
            columns = {column["name"] for column in inspector.get_columns("sendrecord")}
            if "attachment_paths" not in columns:
                conn.execute(text("ALTER TABLE sendrecord ADD COLUMN attachment_paths TEXT"))
        if "draftprepitem" in tables:
            columns = {column["name"] for column in inspector.get_columns("draftprepitem")}
            indexes = {index["name"] for index in inspector.get_indexes("draftprepitem")}
            if "contact_id" in columns and "ix_draftprepitem_contact_id" not in indexes:
                conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_draftprepitem_contact_id ON draftprepitem (contact_id)"))
        if "senderaccount" in tables:
            columns = {column["name"] for column in inspector.get_columns("senderaccount")}
            if "smtp_password_encrypted" not in columns:
                conn.execute(text("ALTER TABLE senderaccount ADD COLUMN smtp_password_encrypted TEXT"))
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


def get_session() -> Generator[Session, None, None]:
    with Session(engine) as session:
        yield session
