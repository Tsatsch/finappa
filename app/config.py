from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    telegram_bot_token: str
    allowed_telegram_user_id: int
    openai_api_key: str
    openai_model: str = "gpt-4o-mini"
    backup_channel_id: int | None = None
    backup_encryption_key: str | None = None
    timezone: str = "Europe/Berlin"
    db_path: str = "/app/data/finappa.db"
    log_level: str = "INFO"

    @model_validator(mode="after")
    def _check_backup_key(self) -> "Settings":
        if self.backup_channel_id is not None and not self.backup_encryption_key:
            raise ValueError(
                "BACKUP_ENCRYPTION_KEY is required when BACKUP_CHANNEL_ID is set"
            )
        return self


def get_settings() -> Settings:
    return Settings()
