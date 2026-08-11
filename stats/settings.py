from pydantic_settings import BaseSettings, SettingsConfigDict


class StatsSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    db_path: str = "/app/data/finappa.db"
    timezone: str = "Europe/Berlin"
    stats_username: str
    stats_password: str
    stats_host: str = "0.0.0.0"
    stats_port: int = 8080


def get_stats_settings() -> StatsSettings:
    return StatsSettings()
