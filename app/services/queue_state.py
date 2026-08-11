from datetime import datetime
from typing import TypedDict

from sqlmodel import Session, select

from app.models import EmailDraft, SenderAccount
from app.services.app_settings import is_queue_paused, set_app_setting


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
    queued_drafts = session.exec(
        select(EmailDraft).where(EmailDraft.status == "queued").order_by(EmailDraft.scheduled_at)
    ).all()
    queued_count = len(queued_drafts)
    paused = is_queue_paused(session)

    if queued_count == 0 and paused:
        set_app_setting(session, "queue_paused", "false", commit=False)
        paused = False
        if commit:
            session.commit()

    next_draft = queued_drafts[0] if queued_drafts else None
    sender_email = None
    if next_draft and next_draft.sender_account_id:
        sender = session.get(SenderAccount, next_draft.sender_account_id)
        sender_email = sender.email if sender else None

    if queued_count == 0:
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
        "has_queue": queued_count > 0,
    }
