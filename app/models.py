from datetime import datetime, time
from typing import Optional
from uuid import uuid4

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from app.time_utils import utc_now


class Company(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    country: str | None = Field(default=None, index=True)
    region: str | None = Field(default=None, index=True)
    company_type: str | None = None
    source: str | None = None
    priority: str = Field(default="B", index=True)
    status: str = Field(default="new", index=True)
    last_contact_at: datetime | None = Field(default=None, index=True)
    next_follow_up_at: datetime | None = Field(default=None, index=True)
    notes: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Contact(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("email", name="uq_contact_email"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    full_name: str
    first_name: str | None = None
    position: str | None = None
    email: str = Field(index=True)
    linkedin: str | None = None
    whatsapp: str | None = None
    priority_contact_rank: int | None = Field(default=None, index=True)
    is_primary: bool = False
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ContactRoute(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("contact_id", "route_type", "route_value", name="uq_contactroute_contact_type_value"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    contact_id: int = Field(foreign_key="contact.id", index=True)
    route_type: str = Field(index=True)
    route_value: str = Field(index=True)
    source: str | None = None
    raw_cell: str | None = Field(default=None, sa_column=Column(Text))
    is_primary: bool = False
    status: str = Field(default="active", index=True)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class DraftPrepItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    contact_id: int = Field(foreign_key="contact.id", index=True, unique=True)
    created_at: datetime = Field(default_factory=utc_now)


class EmailTemplate(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
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


class SenderAccount(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
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
    random_delay_min_seconds: int = 180
    random_delay_max_seconds: int = 600
    enable_open_tracking: bool = False
    is_active: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class EmailDraft(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    contact_id: int = Field(foreign_key="contact.id", index=True)
    template_id: int | None = Field(default=None, foreign_key="emailtemplate.id")
    sender_account_id: int | None = Field(default=None, foreign_key="senderaccount.id")
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


class SendRecord(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    draft_id: int | None = Field(default=None, foreign_key="emaildraft.id", index=True)
    company_id: int = Field(foreign_key="company.id", index=True)
    contact_id: int = Field(foreign_key="contact.id", index=True)
    sender_account_id: int | None = Field(default=None, foreign_key="senderaccount.id", index=True)
    sender_email: str = Field(index=True)
    recipient_email: str = Field(index=True)
    subject: str
    body_html: str = Field(sa_column=Column(Text))
    body_text: str | None = Field(default=None, sa_column=Column(Text))
    cc_emails: str | None = Field(default=None, sa_column=Column(Text))
    attachment_paths: str | None = Field(default=None, sa_column=Column(Text))
    smtp_status: str = Field(default="sent", index=True)
    sent_at: datetime = Field(default_factory=utc_now, index=True)
    created_at: datetime = Field(default_factory=utc_now)


class BounceRecord(SQLModel, table=True):
    __table_args__ = (UniqueConstraint("sender_account_id", "mailbox_uid", "recipient_email", name="uq_bouncerecord_sender_uid_recipient"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    send_record_id: int | None = Field(default=None, foreign_key="sendrecord.id", index=True)
    company_id: int | None = Field(default=None, foreign_key="company.id", index=True)
    contact_id: int | None = Field(default=None, foreign_key="contact.id", index=True)
    sender_account_id: int | None = Field(default=None, foreign_key="senderaccount.id", index=True)
    sender_email: str | None = Field(default=None, index=True)
    recipient_email: str = Field(index=True)
    mailbox_uid: str = Field(index=True)
    bounce_subject: str | None = None
    bounce_from: str | None = None
    bounce_reason: str | None = Field(default=None, sa_column=Column(Text))
    raw_excerpt: str | None = Field(default=None, sa_column=Column(Text))
    detected_at: datetime = Field(default_factory=utc_now, index=True)


class FollowUpRule(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    delay_days: int = 3
    template_id: int = Field(foreign_key="emailtemplate.id")
    priorities: str = "A,B,C"
    is_active: bool = True
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class Suppression(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    email: str = Field(index=True, unique=True)
    reason: str = "blacklist"
    created_at: datetime = Field(default_factory=utc_now)


class EmailEvent(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    draft_id: int | None = Field(default=None, foreign_key="emaildraft.id", index=True)
    event_type: str = Field(index=True)
    occurred_at: datetime = Field(default_factory=utc_now, index=True)
    metadata_json: str | None = Field(default=None, sa_column=Column(Text))


class AppSetting(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    key: str = Field(index=True, unique=True)
    value: str = Field(default="", sa_column=Column(Text))
    updated_at: datetime = Field(default_factory=utc_now)
