import pytest

from src.app import config as config_module


def _set_required_env(monkeypatch):
    monkeypatch.setattr(config_module, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setenv("HASH_SALT", "salt")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setenv("ADMIN_AUTH_TOKEN", "admin")
    monkeypatch.setenv("KAD_API_KEY", "kad_key")
    monkeypatch.setenv("KAD_API_BASE_URL", "https://api.test")


def test_load_config_does_not_require_openrouter_or_postgres(monkeypatch):
    _set_required_env(monkeypatch)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)

    config = config_module.load_config()

    assert config.openrouter_api_key is None
