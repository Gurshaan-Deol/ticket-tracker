from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Notifications
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    alert_email_to: str = ""
    desktop_notifications_enabled: bool = False

    # Alert defaults
    default_alert_cooldown_minutes: int = 60
    default_refresh_interval_minutes: int = 30

    # AI (all optional)
    ai_provider: str = ""
    ai_base_url: str = ""
    ai_api_key: str = ""
    ai_model: str = ""

    # Database
    database_url: str = "sqlite+aiosqlite:///./data/db.sqlite3"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def get_settings() -> Settings:
    return Settings()
