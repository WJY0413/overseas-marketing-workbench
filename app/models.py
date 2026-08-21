from __future__ import annotations

from datetime import datetime, time
from typing import Optional
from uuid import uuid4

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.time_utils import utc_now


def _local_company_id() -> str:
    return f"WB{uuid4().hex[:16].upper()}"


def _local_domain() -> str:
    return f"wb-{uuid4().hex}.email-workbench-local.invalid"


def _local_row_index() -> int:
    return int(uuid4().hex[:12], 16)


class Company(SQLModel, table=True):
    __tablename__ = "companies"

    id: str = Field(default_factory=_local_company_id, sa_column=Column("company_id", String, primary_key=True))
    domain: str = Field(default_factory=_local_domain, sa_column=Column(String, nullable=False, unique=True, index=True))
    name: str = Field(sa_column=Column("standard_company_name", String, index=True))
    website: str | None = None
    country: str | None = Field(default=None, index=True)
    priority: str = Field(default="B", sa_column=Column("rating", String, index=True))
    company_type: str | None = Field(default=None, index=True)
    company_intro: str | None = Field(default=None, sa_column=Column(Text))
    source: str | None = None
    notes: str | None = Field(default=None, sa_column=Column("company_remark", Text))
    status: str = Field(default="new", index=True)
    is_blacklisted: bool = False
    blacklist_reason: str | None = Field(default=None, sa_column=Column(Text))
    connected: bool = False
    region: str | None = Field(default=None, sa_column=Column("primary_business_region", String, index=True))
    branch_location_count: int = 0
    contact_count: int = 0
    source_record_count: int = 0
    activity_record_count: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    raw_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    last_contact_at: datetime | None = Field(default=None, index=True)
    next_follow_up_at: datetime | None = Field(default=None, index=True)
    opportunity_stage: str | None = Field(default=None, index=True)
    crm_owner: str | None = Field(default=None, index=True)


class Contact(SQLModel, table=True):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("primary_email", name="uq_contacts_primary_email"),)

    id: Optional[int] = Field(default=None, sa_column=Column("contact_id", Integer, primary_key=True))
    company_id: str = Field(foreign_key="companies.company_id", index=True)
    domain: str = Field(default="", index=True)
    row_index: int = Field(default_factory=_local_row_index)
    full_name: str = Field(default="Team", sa_column=Column("contact_name", String, index=True))
    position: str | None = Field(default=None, sa_column=Column("contact_position", String))
    contact_route: str | None = Field(default=None, sa_column=Column(Text))
    linkedin_profile_normalized: str | None = None
    linkedin_person_id: str | None = None
    linkedin_mapping_status: str | None = None
    follow_up_record: str | None = Field(default=None, sa_column=Column(Text))
    follow_up_note: str | None = Field(default=None, sa_column=Column(Text))
    email_send_status: str | None = None
    priority_contact_status: str | None = None
    priority_contact_level: str | None = None
    priority_contact_rank: int | None = Field(default=None, index=True)
    priority_roll_status: str | None = None
    upload_id: str | None = None
    source_file: str | None = None
    source_path: str | None = None
    source_sheet: str | None = None
    source_row: str | None = None
    source_label: str | None = None
    imported_at: datetime | None = None
    raw_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    first_name: str | None = None
    email: str = Field(sa_column=Column("primary_email", String, index=True))
    linkedin: str | None = Field(default=None, sa_column=Column("linkedin_url", String))
    whatsapp: str | None = None
    is_primary: bool = False
    status: str = Field(default="active", sa_column=Column("contact_status", String, index=True))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ContactRoute(SQLModel, table=True):
    __tablename__ = "contact_routes"
    __table_args__ = (
        UniqueConstraint("contact_id", "route_type", "route_value", name="uq_contact_routes_contact_type_value"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column("route_id", Integer, primary_key=True))
    contact_id: int = Field(foreign_key="contacts.contact_id", index=True)
    route_type: str = Field(index=True)
    route_value: str = Field(index=True)
    source: str | None = None
    raw_cell: str | None = Field(default=None, sa_column=Column(Text))
    is_primary: bool = False
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DraftPrepItem(SQLModel, table=True):
    __tablename__ = "draft_prep_items"

    id: Optional[int] = Field(default=None, sa_column=Column("prep_id", Integer, primary_key=True))
    contact_id: int = Field(foreign_key="contacts.contact_id", index=True, unique=True)
    created_at: datetime = Field(default_factory=utc_now)


class EmailTemplate(SQLModel, table=True):
    __tablename__ = "email_templates"

    id: Optional[int] = Field(default=None, sa_column=Column("template_id", Integer, primary_key=True))
    name: str = Field(index=True)
    template_type: str = Field(default="first_touch", index=True)
    subject: str
    body_html: str = Field(sa_column=Column(Text))
    body_text: str | None = Field(default=None, sa_column=Column(Text))
    cc_enabled: bool = False
    cc_emails: str | None = Field(default=None, sa_column=Column(Text))
    is_active: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TemplateRotationPolicy(SQLModel, table=True):
    """Durable planner policy; never infer compatibility from template IDs."""

    __tablename__ = "template_rotation_policies"
    __table_args__ = (UniqueConstraint("template_id", name="uq_template_rotation_policy_template"),)

    id: Optional[int] = Field(default=None, sa_column=Column("policy_id", Integer, primary_key=True))
    template_id: int = Field(foreign_key="email_templates.template_id", index=True)
    scope: str = Field(default="general", index=True)
    match_keywords: str | None = Field(default=None, sa_column=Column(Text))
    priority: int = 100
    is_enabled: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class SenderAccount(SQLModel, table=True):
    __tablename__ = "sender_accounts"

    id: Optional[int] = Field(default=None, sa_column=Column("sender_account_id", Integer, primary_key=True))
    name: str
    email: str = Field(index=True)
    smtp_host: str
    smtp_port: int = 587
    smtp_username: str | None = None
    password_env: str | None = None
    smtp_password_encrypted: str | None = Field(default=None, sa_column=Column(Text))
    daily_limit: int = 30
    window_start: time = time(9, 30)
    window_end: time = time(17, 30)
    send_timezone: str | None = None
    send_windows_json: str | None = Field(default=None, sa_column=Column(Text))
    random_delay_min_seconds: int = 180
    random_delay_max_seconds: int = 600
    enable_open_tracking: bool = False
    is_active: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class EmailDraft(SQLModel, table=True):
    __tablename__ = "email_drafts"

    id: Optional[int] = Field(default=None, sa_column=Column("draft_id", Integer, primary_key=True))
    company_id: str = Field(foreign_key="companies.company_id", index=True)
    contact_id: int = Field(foreign_key="contacts.contact_id", index=True)
    template_id: int | None = Field(default=None, foreign_key="email_templates.template_id")
    signature_template_id: int | None = Field(default=None, foreign_key="email_templates.template_id", index=True)
    sender_account_id: int | None = Field(default=None, foreign_key="sender_accounts.sender_account_id")
    subject: str
    body_html: str = Field(sa_column=Column(Text))
    body_text: str | None = Field(default=None, sa_column=Column(Text))
    cc_emails: str | None = Field(default=None, sa_column=Column(Text))
    attachment_paths: str | None = Field(default=None, sa_column=Column(Text))
    status: str = Field(default="pending_review", index=True)
    scheduled_at: datetime | None = Field(default=None, index=True)
    approved_at: datetime | None = Field(default=None, index=True)
    sent_at: datetime | None = Field(default=None, index=True)
    follow_up_step: int = 0
    tracking_id: str = Field(default_factory=lambda: str(uuid4()), index=True, unique=True)
    template_snapshot: str | None = Field(default=None, sa_column=Column(Text))
    error_message: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class QueueHandoffRun(SQLModel, table=True):
    """Durable cursor for a one-click, chunked queue intake."""

    __tablename__ = "queue_handoff_runs"

    id: str = Field(default_factory=lambda: str(uuid4()), sa_column=Column("handoff_id", String, primary_key=True))
    draft_ids_json: str = Field(default="[]", sa_column=Column(Text, nullable=False))
    sender_ids_json: str = Field(default="[]", sa_column=Column(Text, nullable=False))
    template_id: int | None = Field(default=None, foreign_key="email_templates.template_id", index=True)
    send_mode: str = Field(default="single")
    allow_recent_duplicate: bool = False
    batch_size: int = 50
    cursor: int = 0
    total_count: int = 0
    queued_count: int = 0
    excluded_count: int = 0
    status: str = Field(default="pending", index=True)
    last_error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utc_now, index=True)
    updated_at: datetime = Field(default_factory=utc_now)


class SendRecord(SQLModel, table=True):
    __tablename__ = "activity_records"

    id: Optional[int] = Field(default=None, sa_column=Column("activity_id", Integer, primary_key=True))
    company_id: str = Field(foreign_key="companies.company_id", index=True)
    domain: str = Field(default="")
    row_index: int = Field(default_factory=_local_row_index)
    activity_key: str = Field(default_factory=lambda: f"bd_email_workbench:event:{uuid4()}", index=True)
    activity_type: str = Field(default="email_sent", index=True)
    activity_date: datetime | None = None
    sent_at: datetime = Field(default_factory=utc_now, index=True)
    source_system: str = "bd_email_workbench"
    source_record_id: str = Field(default_factory=lambda: str(uuid4()))
    batch_id: str | None = None
    sender_email: str = Field(index=True)
    recipient_email: str = Field(index=True)
    recipient_domain: str = ""
    subject: str
    smtp_status: str = Field(default="sent", index=True)
    created_at: datetime = Field(default_factory=utc_now, sa_column=Column("imported_at", DateTime))
    raw_json: str = Field(default="{}", sa_column=Column(Text, nullable=False))
    contact_id: int = Field(foreign_key="contacts.contact_id", index=True)
    draft_id: int | None = Field(default=None, foreign_key="email_drafts.draft_id", index=True)
    sender_account_id: int | None = Field(default=None, foreign_key="sender_accounts.sender_account_id", index=True)
    template_id: int | None = Field(default=None, foreign_key="email_templates.template_id", index=True)
    signature_template_id: int | None = Field(default=None, foreign_key="email_templates.template_id", index=True)
    body_html: str = Field(default="", sa_column=Column(Text))
    body_text: str | None = Field(default=None, sa_column=Column(Text))
    cc_emails: str | None = Field(default=None, sa_column=Column(Text))
    attachment_paths: str | None = Field(default=None, sa_column=Column(Text))
    error_message: str | None = Field(default=None, sa_column=Column(Text))


class BounceRecord(SQLModel, table=True):
    __tablename__ = "bounce_records"
    __table_args__ = (
        UniqueConstraint("sender_account_id", "mailbox_uid", "recipient_email", name="uq_bounce_sender_uid_recipient"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column("bounce_id", Integer, primary_key=True))
    send_record_id: int | None = Field(
        default=None,
        sa_column=Column("activity_id", Integer, ForeignKey("activity_records.activity_id"), index=True),
    )
    source_send_record_id: int | None = None
    company_id: str | None = Field(default=None, foreign_key="companies.company_id", index=True)
    contact_id: int | None = Field(default=None, foreign_key="contacts.contact_id", index=True)
    sender_account_id: int | None = Field(default=None, foreign_key="sender_accounts.sender_account_id", index=True)
    sender_email: str | None = Field(default=None, index=True)
    recipient_email: str = Field(index=True)
    mailbox_uid: str = Field(index=True)
    bounce_subject: str | None = None
    bounce_from: str | None = None
    bounce_reason: str | None = Field(default=None, sa_column=Column(Text))
    raw_excerpt: str | None = Field(default=None, sa_column=Column(Text))
    detected_at: datetime = Field(default_factory=utc_now, index=True)


class FollowUpRule(SQLModel, table=True):
    __tablename__ = "follow_up_rules"

    id: Optional[int] = Field(default=None, sa_column=Column("rule_id", Integer, primary_key=True))
    name: str
    delay_days: int = 3
    template_id: int = Field(foreign_key="email_templates.template_id")
    priorities: str = "A,B,C"
    is_active: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Suppression(SQLModel, table=True):
    __tablename__ = "suppressions"

    id: Optional[int] = Field(default=None, sa_column=Column("suppression_id", Integer, primary_key=True))
    email: str = Field(index=True, unique=True)
    reason: str = "blacklist"
    expires_at: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)


class EmailEvent(SQLModel, table=True):
    __tablename__ = "email_events"

    id: Optional[int] = Field(default=None, sa_column=Column("event_id", Integer, primary_key=True))
    draft_id: int | None = Field(default=None, foreign_key="email_drafts.draft_id", index=True)
    source_draft_id: int | None = None
    activity_id: int | None = Field(default=None, foreign_key="activity_records.activity_id", index=True)
    event_type: str = Field(index=True)
    occurred_at: datetime = Field(default_factory=utc_now, index=True)
    metadata_json: str | None = Field(default=None, sa_column=Column(Text))


class AppSetting(SQLModel, table=True):
    __tablename__ = "app_settings"

    id: Optional[int] = Field(default=None, sa_column=Column("setting_id", Integer, primary_key=True))
    key: str = Field(index=True, unique=True)
    value: str = Field(default="", sa_column=Column(Text))
    updated_at: datetime = Field(default_factory=utc_now)


class CompanySourceLink(SQLModel, table=True):
    __tablename__ = "source_identity_links"
    __table_args__ = (
        UniqueConstraint("source_system", "external_company_id", name="uq_source_links_source_external"),
        UniqueConstraint("source_system", "company_id", name="uq_source_links_source_company"),
    )

    id: Optional[int] = Field(default=None, sa_column=Column("link_id", Integer, primary_key=True))
    source_system: str = Field(default="BDdb", index=True)
    external_company_id: str = Field(index=True)
    company_id: str = Field(foreign_key="companies.company_id", index=True)
    source_domain: str | None = Field(default=None, index=True)
    match_method: str = Field(default="external_id")
    is_blocked: bool = Field(default=False, index=True)
    last_seen_fingerprint: str | None = Field(default=None, index=True)
    last_synced_at: datetime | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class BdMasterSyncRun(SQLModel, table=True):
    __tablename__ = "master_sync_runs"

    id: str = Field(sa_column=Column("run_id", String, primary_key=True))
    source_system: str = Field(default="BDdb", index=True)
    source_path: str = Field(default="", sa_column=Column(Text))
    source_fingerprint: str = Field(default="", index=True)
    source_mtime_ns: int | None = None
    dry_run: bool = Field(default=False, index=True)
    status: str = Field(index=True)
    source_company_count: int = 0
    inserted_companies: int = 0
    updated_companies: int = 0
    noop_companies: int = 0
    inserted_contacts: int = 0
    updated_contacts: int = 0
    noop_contacts: int = 0
    inserted_routes: int = 0
    inserted_links: int = 0
    reject_count: int = 0
    conflict_count: int = 0
    quarantine_company_count: int = 0
    quarantine_email_count: int = 0
    fatal_conflict_count: int = 0
    error_text: str | None = Field(default=None, sa_column=Column(Text))
    details_json: str | None = Field(default=None, sa_column=Column(Text))
    started_at: datetime = Field(default_factory=utc_now, index=True)
    finished_at: datetime | None = Field(default=None, index=True)


class BdMasterSyncConflict(SQLModel, table=True):
    __tablename__ = "master_sync_conflicts"

    id: Optional[int] = Field(default=None, sa_column=Column("conflict_id", Integer, primary_key=True))
    run_id: str = Field(foreign_key="master_sync_runs.run_id", index=True)
    conflict_type: str = Field(index=True)
    external_company_id: str | None = Field(default=None, index=True)
    domain: str | None = Field(default=None, index=True)
    email: str | None = Field(default=None, index=True)
    status: str = Field(default="blocked", index=True)
    details_json: str = Field(default="{}", sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utc_now, index=True)
