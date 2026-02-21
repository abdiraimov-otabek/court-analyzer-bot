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
    kad_api_base_url: str | None
    kad_api_key: str | None
    admin_auth_token: str | None
    openrouter_api_key: str | None = None


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
        kad_api_base_url=os.getenv(
            "KAD_API_BASE_URL", "https://parser-api.com/parser/arbitr_api"
        ),
        kad_api_key=os.getenv("KAD_API_KEY"),
        admin_auth_token=os.getenv("ADMIN_AUTH_TOKEN"),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
    )

    # Phase 3.9: Validate mandatory environment variables
    missing = []
    if not config.hash_salt:
        missing.append("HASH_SALT")
    if not config.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config.kad_api_key:
        missing.append("KAD_API_KEY")
    if not config.admin_auth_token:
        missing.append("ADMIN_AUTH_TOKEN")
    if not config.openrouter_api_key:
        missing.append("OPENROUTER_API_KEY")

    if missing:
        raise ValueError(
            f"Missing mandatory environment variables: {', '.join(missing)}"
        )

    return config
