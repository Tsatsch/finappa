import pytest

from app.config import Settings
from app.main import build_application


@pytest.fixture
def settings(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:fake-token-for-tests")
    monkeypatch.setenv("ALLOWED_TELEGRAM_USER_ID", "12345")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "finappa.db"))
    return Settings(_env_file=None)


def test_build_application_wires_bot_data_and_jobs(settings, tmp_path):
    application = build_application(settings, recurring_yaml_path=str(tmp_path / "missing.yml"))

    assert application.bot_data["timezone"] == "Europe/Berlin"
    assert application.bot_data["openai_model"] == "gpt-4o-mini"
    assert "session_factory" in application.bot_data

    job_names = {job.name for job in application.job_queue.jobs()}
    assert {"recurring_check", "recurring_check_startup", "daily_backup"} <= job_names
