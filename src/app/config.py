from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    database_path: str
    hash_salt: str
    telegram_bot_token: str | None
    admin_auth_token: str | None
    kad_api_base_url: str
    kad_api_key: str
    openrouter_api_key: str | None = None  # Optional: enables AI-driven analysis features
    decision_source_mode: str = "kad"
    shadow_mode_enabled: bool = False
    captcha_solver_api_key: str | None = None  # Optional: enables CAPTCHA auto-solving (e.g. 2Captcha key)
    captcha_solver_url: str | None = None  # Optional: custom CAPTCHA solver API base URL
    captcha_solver_timeout: int = 180  # Max seconds to wait for CAPTCHA solution


def load_config() -> AppConfig:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    load_dotenv(dotenv_path=env_path)
    raw_database_path = os.getenv("DATABASE_PATH", "data/app.db")
    database_path = Path(raw_database_path).expanduser()
    if not database_path.is_absolute():
        database_path = (env_path.parent / database_path).resolve()
    
    config = AppConfig(
        database_path=str(database_path),
        hash_salt=os.getenv("HASH_SALT", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
        admin_auth_token=os.getenv("ADMIN_AUTH_TOKEN"),
        kad_api_base_url=os.getenv("KAD_API_BASE_URL", "https://kad.arbitr.ru/"),
        kad_api_key=os.getenv("KAD_API_KEY", ""),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
        decision_source_mode=os.getenv("DECISION_SOURCE_MODE", "kad"),
        shadow_mode_enabled=os.getenv("SHADOW_MODE_ENABLED", "false").lower() == "true",
        captcha_solver_api_key=os.getenv("CAPTCHA_SOLVER_API_KEY"),
        captcha_solver_url=os.getenv("CAPTCHA_SOLVER_URL"),
        captcha_solver_timeout=int(os.getenv("CAPTCHA_SOLVER_TIMEOUT", "180")),
    )

    # Validate mandatory environment variables used by the default runtime.
    missing = []
    if not config.hash_salt:
        missing.append("HASH_SALT")
    if not config.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config.admin_auth_token:
        missing.append("ADMIN_AUTH_TOKEN")
    if not config.kad_api_base_url:
        missing.append("KAD_API_BASE_URL")
    if not config.kad_api_key:
        missing.append("KAD_API_KEY")

    if missing:
        raise ValueError(
            f"Missing mandatory environment variables: {', '.join(missing)}"
        )

    return config


def initialize_db(config: AppConfig) -> None:
    """
    Ensures that the database and its schema are initialized.
    """
    from src.infrastructure.sqlite import SqliteConnection
    # SqliteConnection constructor handles _ensure_schema
    SqliteConnection(config.database_path)
