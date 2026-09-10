from datetime import datetime
from typing import TypedDict

from sqlmodel import Session, select
from sqlalchemy import func

from app.models import EmailDraft, QueueHandoffRun, SenderAccount
from app.services.app_settings import is_queue_paused


class QueueState(TypedDict):
    status: str
    label: str
    button_label: str
    action_url: str
    queued_count: int
    next_scheduled_at: datetime | None
    sender_email: str | None
    is_paused: bool
    has_queue: bool


def reconcile_queue_state(session: Session, commit: bool = True) -> QueueState:
    queued_count = session.exec(select(func.count(EmailDraft.id)).where(EmailDraft.status == "queued")).one()
    pending_handoffs = session.exec(select(func.count(QueueHandoffRun.id)).where(
        QueueHandoffRun.status.in_(("pending", "running")))).one()
    paused = is_queue_paused(session)
    next_draft = session.exec(
        select(EmailDraft.scheduled_at, EmailDraft.sender_account_id)
        .where(EmailDraft.status == "queued").order_by(EmailDraft.scheduled_at).limit(1)
    ).first() if queued_count else None
    sender_email = None
    if next_draft and next_draft.sender_account_id:
        sender = session.get(SenderAccount, next_draft.sender_account_id)
        sender_email = sender.email if sender else None

    if queued_count == 0 and pending_handoffs == 0:
        status = "idle"
        label = "未开始"
        button_label = "开始发送"
        action_url = "/send/start"
    elif paused:
        status = "paused"
        label = "已暂停"
        button_label = "继续发送"
        action_url = "/queue/resume"
    else:
        status = "running"
        label = "发送中"
        button_label = "停止发送"
        action_url = "/queue/pause"

    return {
        "status": status,
        "label": label,
        "button_label": button_label,
        "action_url": action_url,
        "queued_count": queued_count,
        "next_scheduled_at": next_draft.scheduled_at if next_draft else None,
        "sender_email": sender_email,
        "is_paused": paused,
        "has_queue": queued_count > 0 or pending_handoffs > 0,
    }
