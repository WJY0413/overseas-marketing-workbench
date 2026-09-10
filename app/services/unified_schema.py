from __future__ import annotations

import sqlite3


SCHEMA_NAME = "bddb-workbench-unified"
SCHEMA_VERSION = 1


COMPANY_EXTENSION_COLUMNS = {
    "customer_library": "TEXT NOT NULL DEFAULT 'unclassified'",
    "last_contact_at": "TEXT",
    "next_follow_up_at": "TEXT",
    "opportunity_stage": "TEXT",
    "crm_owner": "TEXT",
}

CONTACT_EXTENSION_COLUMNS = {
    "first_name": "TEXT",
    "primary_email": "TEXT",
    "linkedin_url": "TEXT",
    "whatsapp": "TEXT",
    "is_primary": "INTEGER NOT NULL DEFAULT 0",
    "contact_status": "TEXT",
    "created_at": "TEXT",
    "updated_at": "TEXT",
}

ACTIVITY_EXTENSION_COLUMNS = {
    "contact_id": "INTEGER",
    "draft_id": "INTEGER",
    "sender_account_id": "INTEGER",
    "template_id": "INTEGER",
    "signature_template_id": "INTEGER",
    "body_html": "TEXT",
    "body_text": "TEXT",
    "cc_emails": "TEXT",
    "attachment_paths": "TEXT",
    "error_message": "TEXT",
}


OPERATIONAL_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS unified_schema_version (
    schema_name TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS contact_routes (
    route_id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL,
    route_type TEXT NOT NULL,
    route_value TEXT NOT NULL,
    source TEXT,
    raw_cell TEXT,
    is_primary INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY(contact_id) REFERENCES contacts(contact_id),
    UNIQUE(contact_id, route_type, route_value)
);

CREATE TABLE IF NOT EXISTS draft_prep_items (
    prep_id INTEGER PRIMARY KEY,
    contact_id INTEGER NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY(contact_id) REFERENCES contacts(contact_id)
);

CREATE TABLE IF NOT EXISTS email_templates (
    template_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    template_type TEXT NOT NULL,
    subject TEXT NOT NULL,
    body_html TEXT,
    body_text TEXT,
    cc_enabled INTEGER NOT NULL DEFAULT 0,
    cc_emails TEXT,
    target_countries TEXT NOT NULL DEFAULT '通用',
    target_types TEXT NOT NULL DEFAULT '通用',
    product_tags TEXT NOT NULL DEFAULT '通用',
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS sender_accounts (
    sender_account_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT NOT NULL,
    smtp_host TEXT NOT NULL,
    smtp_port INTEGER NOT NULL,
    smtp_username TEXT,
    password_env TEXT,
    smtp_password_encrypted TEXT,
    daily_limit INTEGER NOT NULL DEFAULT 30,
    window_start TEXT,
    window_end TEXT,
    send_timezone TEXT,
    send_windows_json TEXT,
    random_delay_min_seconds INTEGER NOT NULL DEFAULT 180,
    random_delay_max_seconds INTEGER NOT NULL DEFAULT 600,
    enable_open_tracking INTEGER NOT NULL DEFAULT 0,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS email_drafts (
    draft_id INTEGER PRIMARY KEY,
    company_id TEXT NOT NULL,
    contact_id INTEGER NOT NULL,
    template_id INTEGER,
    signature_template_id INTEGER,
    sender_account_id INTEGER,
    subject TEXT NOT NULL,
    body_html TEXT,
    body_text TEXT,
    cc_emails TEXT,
    attachment_paths TEXT,
    status TEXT NOT NULL,
    scheduled_at TEXT,
    approved_at TEXT,
    sent_at TEXT,
    follow_up_step INTEGER NOT NULL DEFAULT 0,
    tracking_id TEXT NOT NULL,
    template_snapshot TEXT,
    error_message TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY(company_id) REFERENCES companies(company_id),
    FOREIGN KEY(contact_id) REFERENCES contacts(contact_id),
    FOREIGN KEY(template_id) REFERENCES email_templates(template_id),
    FOREIGN KEY(signature_template_id) REFERENCES email_templates(template_id),
    FOREIGN KEY(sender_account_id) REFERENCES sender_accounts(sender_account_id)
);

CREATE TABLE IF NOT EXISTS bounce_records (
    bounce_id INTEGER PRIMARY KEY,
    activity_id INTEGER,
    source_send_record_id INTEGER,
    company_id TEXT,
    contact_id INTEGER,
    sender_account_id INTEGER,
    sender_email TEXT,
    recipient_email TEXT NOT NULL,
    mailbox_uid TEXT NOT NULL,
    bounce_subject TEXT,
    bounce_from TEXT,
    bounce_reason TEXT,
    raw_excerpt TEXT,
    detected_at TEXT NOT NULL,
    FOREIGN KEY(activity_id) REFERENCES activity_records(activity_id),
    FOREIGN KEY(company_id) REFERENCES companies(company_id),
    FOREIGN KEY(contact_id) REFERENCES contacts(contact_id),
    FOREIGN KEY(sender_account_id) REFERENCES sender_accounts(sender_account_id),
    UNIQUE(sender_account_id, mailbox_uid, recipient_email)
);

CREATE TABLE IF NOT EXISTS suppressions (
    suppression_id INTEGER PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS email_events (
    event_id INTEGER PRIMARY KEY,
    draft_id INTEGER,
    source_draft_id INTEGER,
    activity_id INTEGER,
    event_type TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    metadata_json TEXT,
    FOREIGN KEY(draft_id) REFERENCES email_drafts(draft_id),
    FOREIGN KEY(activity_id) REFERENCES activity_records(activity_id)
);

CREATE TABLE IF NOT EXISTS follow_up_rules (
    rule_id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    delay_days INTEGER NOT NULL,
    template_id INTEGER NOT NULL,
    priorities TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 0,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY(template_id) REFERENCES email_templates(template_id)
);

CREATE TABLE IF NOT EXISTS app_settings (
    setting_id INTEGER PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    value TEXT,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_identity_links (
    link_id INTEGER PRIMARY KEY,
    source_system TEXT NOT NULL,
    external_company_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    source_domain TEXT,
    match_method TEXT NOT NULL,
    is_blocked INTEGER NOT NULL DEFAULT 0,
    last_seen_fingerprint TEXT,
    last_synced_at TEXT,
    created_at TEXT,
    updated_at TEXT,
    FOREIGN KEY(company_id) REFERENCES companies(company_id),
    UNIQUE(source_system, external_company_id),
    UNIQUE(source_system, company_id)
);

CREATE TABLE IF NOT EXISTS master_sync_runs (
    run_id TEXT PRIMARY KEY,
    source_system TEXT NOT NULL,
    source_path TEXT,
    source_fingerprint TEXT NOT NULL,
    source_mtime_ns INTEGER,
    dry_run INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL,
    source_company_count INTEGER NOT NULL DEFAULT 0,
    inserted_companies INTEGER NOT NULL DEFAULT 0,
    updated_companies INTEGER NOT NULL DEFAULT 0,
    noop_companies INTEGER NOT NULL DEFAULT 0,
    inserted_contacts INTEGER NOT NULL DEFAULT 0,
    updated_contacts INTEGER NOT NULL DEFAULT 0,
    noop_contacts INTEGER NOT NULL DEFAULT 0,
    inserted_routes INTEGER NOT NULL DEFAULT 0,
    inserted_links INTEGER NOT NULL DEFAULT 0,
    reject_count INTEGER NOT NULL DEFAULT 0,
    conflict_count INTEGER NOT NULL DEFAULT 0,
    quarantine_company_count INTEGER NOT NULL DEFAULT 0,
    quarantine_email_count INTEGER NOT NULL DEFAULT 0,
    fatal_conflict_count INTEGER NOT NULL DEFAULT 0,
    error_text TEXT,
    details_json TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE IF NOT EXISTS master_sync_conflicts (
    conflict_id INTEGER PRIMARY KEY,
    run_id TEXT NOT NULL,
    conflict_type TEXT NOT NULL,
    external_company_id TEXT,
    domain TEXT,
    email TEXT,
    status TEXT NOT NULL,
    details_json TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES master_sync_runs(run_id)
);

CREATE TABLE IF NOT EXISTS migration_entity_map (
    map_id INTEGER PRIMARY KEY,
    source_system TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    match_method TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(source_system, entity_type, source_id)
);

CREATE TABLE IF NOT EXISTS migration_conflicts (
    conflict_id INTEGER PRIMARY KEY,
    source_system TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    source_id TEXT,
    conflict_type TEXT NOT NULL,
    details_json TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_contacts_primary_email ON contacts(primary_email);
CREATE INDEX IF NOT EXISTS idx_companies_customer_library ON companies(customer_library);
CREATE INDEX IF NOT EXISTS idx_contact_routes_contact_type ON contact_routes(contact_id, route_type);
CREATE INDEX IF NOT EXISTS idx_email_drafts_status_schedule ON email_drafts(status, scheduled_at);
CREATE INDEX IF NOT EXISTS idx_email_drafts_company ON email_drafts(company_id);
CREATE INDEX IF NOT EXISTS idx_email_drafts_contact ON email_drafts(contact_id);
CREATE INDEX IF NOT EXISTS idx_activity_source_record ON activity_records(source_system, source_record_id);
CREATE INDEX IF NOT EXISTS idx_activity_contact ON activity_records(contact_id);
CREATE INDEX IF NOT EXISTS idx_bounce_recipient ON bounce_records(recipient_email);
CREATE INDEX IF NOT EXISTS idx_email_events_draft_time ON email_events(draft_id, occurred_at);
CREATE INDEX IF NOT EXISTS idx_migration_conflicts_status ON migration_conflicts(review_status, conflict_type);
"""


def _table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _require_base_schema(connection: sqlite3.Connection) -> None:
    required = {"companies", "contacts", "activity_records"}
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    missing = sorted(required - tables)
    if missing:
        raise ValueError(f"Missing BDdb base tables: {', '.join(missing)}")


def _add_columns(connection: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    existing = _table_columns(connection, table)
    for name, ddl in columns.items():
        if name not in existing:
            connection.execute(f'ALTER TABLE "{table}" ADD COLUMN "{name}" {ddl}')


def ensure_unified_schema(connection: sqlite3.Connection) -> None:
    _require_base_schema(connection)
    _add_columns(connection, "companies", COMPANY_EXTENSION_COLUMNS)
    _add_columns(connection, "contacts", CONTACT_EXTENSION_COLUMNS)
    _add_columns(connection, "activity_records", ACTIVITY_EXTENSION_COLUMNS)
    for column in ("activity_date", "sent_at", "imported_at"):
        connection.execute(
            f'UPDATE activity_records SET "{column}"=NULL WHERE TRIM(COALESCE("{column}", \'\'))=\'\''
        )
    connection.executescript(OPERATIONAL_SCHEMA_SQL)
    _add_columns(connection, "bounce_records", {"source_send_record_id": "INTEGER"})
    _add_columns(connection, "email_events", {"source_draft_id": "INTEGER"})
    connection.execute(
        """
        INSERT INTO unified_schema_version(schema_name, version, applied_at)
        VALUES (?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(schema_name) DO UPDATE SET
            version=excluded.version,
            applied_at=excluded.applied_at
        """,
        (SCHEMA_NAME, SCHEMA_VERSION),
    )


def schema_version(connection: sqlite3.Connection) -> int | None:
    tables = {
        row[0]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "unified_schema_version" not in tables:
        return None
    row = connection.execute(
        "SELECT version FROM unified_schema_version WHERE schema_name = ?",
        (SCHEMA_NAME,),
    ).fetchone()
    return int(row[0]) if row else None
