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
    # The operational BDdb read model is SQLite. JSON remains an explicit
    # compatibility input for archived/legacy exports only.
    bd_database_sqlite_path: str | None = None
    bd_database_json_path: str | None = None
    linkedin_master_sqlite_path: str | None = None

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

    @property
    def bd_database_path(self) -> Path | None:
        if self.bd_database_sqlite_path:
            return Path(self.bd_database_sqlite_path).expanduser()
        if self.bd_database_json_path:
            return Path(self.bd_database_json_path).expanduser()
        database_dir = (
            Path.home()
            / "Documents"
            / "Fjd sales"
            / "bd_company_database"
            / "database"
        )
        sqlite_source = database_dir / "bd_company_database.sqlite"
        if sqlite_source.is_file():
            return sqlite_source
        legacy_json_source = database_dir / "bd_company_database.json"
        return legacy_json_source if legacy_json_source.is_file() else None

    @property
    def linkedin_master_path(self) -> Path:
        return Path(self.linkedin_master_sqlite_path or r"C:\Users\22524\Documents\Personal agent\linkedin_people_master.sqlite").expanduser()


@lru_cache
def get_settings() -> Settings:
    return Settings()
