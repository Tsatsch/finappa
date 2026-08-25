import pytest
from pydantic import ValidationError

from app.config import Settings


def _base_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "12345")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")


def test_settings_load_with_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    settings = Settings(_env_file=None)
    assert settings.openai_model == "gpt-5.6-luna"
    assert settings.openai_reasoning_effort == "none"
    assert settings.openai_model_heavy == "gpt-5.6-terra"
    assert settings.openai_reasoning_effort_heavy == "medium"
    assert settings.timezone == "Europe/Berlin"
    assert settings.backup_channel_id is None


def test_openai_model_overrides(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("OPENAI_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT", "low")
    monkeypatch.setenv("OPENAI_MODEL_HEAVY", "gpt-5.6-sol")
    monkeypatch.setenv("OPENAI_REASONING_EFFORT_HEAVY", "high")
    settings = Settings(_env_file=None)
    assert settings.openai_model == "gpt-5.6-luna"
    assert settings.openai_reasoning_effort == "low"
    assert settings.openai_model_heavy == "gpt-5.6-sol"
    assert settings.openai_reasoning_effort_heavy == "high"


def test_settings_missing_required_field_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "12345")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_backup_channel_without_key_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("BACKUP_CHANNEL_ID", "-1001234567890")
    monkeypatch.delenv("BACKUP_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValidationError, match="BACKUP_ENCRYPTION_KEY"):
        Settings(_env_file=None)


def test_backup_channel_with_key_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    _base_env(monkeypatch)
    monkeypatch.setenv("BACKUP_CHANNEL_ID", "-1001234567890")
    monkeypatch.setenv("BACKUP_ENCRYPTION_KEY", "fake-fernet-key")
    settings = Settings(_env_file=None)
    assert settings.backup_channel_id == -1001234567890
