from sqlmodel import Session, select

from app.models import AppSetting
from app.time_utils import utc_now


def get_app_setting(session: Session, key: str, default: str = "") -> str:
    setting = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
    return setting.value if setting else default


def set_app_setting(session: Session, key: str, value: str, commit: bool = True) -> AppSetting:
    setting = session.exec(select(AppSetting).where(AppSetting.key == key)).first()
    if setting is None:
        setting = AppSetting(key=key)
    setting.value = value
    setting.updated_at = utc_now()
    session.add(setting)
    if commit:
        session.commit()
        session.refresh(setting)
    return setting


def is_queue_paused(session: Session) -> bool:
    return get_app_setting(session, "queue_paused", "false").lower() == "true"
