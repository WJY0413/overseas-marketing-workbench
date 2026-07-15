import ctypes
import platform
from typing import TypedDict

from sqlalchemy import func
from sqlmodel import Session, select

from app.models import EmailDraft
from app.services.app_settings import is_queue_paused


ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class PowerAwakeState(TypedDict):
    supported: bool
    active: bool
    queued_count: int
    paused: bool
    error: str | None


_last_active = False


def _set_thread_execution_state(flags: int) -> bool:
    result = ctypes.windll.kernel32.SetThreadExecutionState(flags)
    return result != 0


def _is_supported() -> bool:
    return platform.system().lower() == "windows"


def keep_system_awake() -> PowerAwakeState:
    global _last_active
    if not _is_supported():
        _last_active = False
        return {"supported": False, "active": False, "queued_count": 0, "paused": False, "error": None}
    try:
        ok = _set_thread_execution_state(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
    except Exception as exc:
        _last_active = False
        return {"supported": True, "active": False, "queued_count": 0, "paused": False, "error": str(exc)}
    _last_active = ok
    return {"supported": True, "active": ok, "queued_count": 0, "paused": False, "error": None if ok else "SetThreadExecutionState failed."}


def release_system_awake() -> PowerAwakeState:
    global _last_active
    if not _is_supported():
        _last_active = False
        return {"supported": False, "active": False, "queued_count": 0, "paused": False, "error": None}
    try:
        ok = _set_thread_execution_state(ES_CONTINUOUS)
    except Exception as exc:
        return {"supported": True, "active": _last_active, "queued_count": 0, "paused": False, "error": str(exc)}
    _last_active = False if ok else _last_active
    return {"supported": True, "active": _last_active, "queued_count": 0, "paused": False, "error": None if ok else "SetThreadExecutionState release failed."}


def sync_power_awake(session: Session) -> PowerAwakeState:
    queued_count = session.exec(select(func.count(EmailDraft.id)).where(EmailDraft.status == "queued")).one()
    paused = is_queue_paused(session)
    if queued_count > 0 and not paused:
        state = keep_system_awake()
    else:
        state = release_system_awake()
    state["queued_count"] = queued_count
    state["paused"] = paused
    return state
