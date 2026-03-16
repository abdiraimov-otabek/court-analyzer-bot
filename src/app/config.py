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
        admin_auth_token=os.getenv("ADMIN_AUTH_TOKEN"),
        openrouter_api_key=os.getenv("OPENROUTER_API_KEY"),
    )

    # Validate mandatory environment variables used by the default runtime.
    missing = []
    if not config.hash_salt:
        missing.append("HASH_SALT")
    if not config.telegram_bot_token:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not config.admin_auth_token:
        missing.append("ADMIN_AUTH_TOKEN")

    if missing:
        raise ValueError(
            f"Missing mandatory environment variables: {', '.join(missing)}"
        )

    return config
