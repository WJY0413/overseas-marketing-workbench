from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str | None = None
    workbench_data_dir: str | None = None
    app_timezone: str = "Asia/Shanghai"
    public_base_url: str = "http://localhost:8000"
    app_environment: str = "local"
    app_instance_name: str = "本地版本"
    enable_open_tracking: bool = False
    bounce_scan_enabled: bool = False
    bounce_scan_interval_minutes: int = 15
    bounce_scan_lookback_days: int = 14
    default_imap_host: str = "imap.feishu.cn"
    default_imap_port: int = 993
    dry_run_email: bool = True
    default_sender_name: str = "Your Name"

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    @property
    def data_dir(self) -> Path:
        return Path(self.workbench_data_dir or "./data")

    @property
    def reports_dir(self) -> Path:
        return self.data_dir / "reports"

    @property
    def resolved_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        database_path = self.data_dir / "workbench.db"
        return f"sqlite:///{database_path.as_posix()}"


@lru_cache
def get_settings() -> Settings:
    return Settings()
