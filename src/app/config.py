from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
import peewee as pw

DATABASE = pw.PostgresqlDatabase(None)


@dataclass(frozen=True)
class AppConfig:
    database_path: str
    hash_salt: str
    telegram_bot_token: str | None
    admin_auth_token: str | None
    openrouter_api_key: str | None = None
    pg_db_name: str | None = None
    pg_db_user: str | None = None
    pg_db_password: str | None = None
    pg_db_host: str | None = None
    pg_db_port: str | None = None


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
        pg_db_name=os.getenv("PG_DB_NAME"),
        pg_db_user=os.getenv("PG_DB_USER"),
        pg_db_password=os.getenv("PG_DB_PASSWORD"),
        pg_db_host=os.getenv("PG_DB_HOST", "localhost"),
        pg_db_port=os.getenv("PG_DB_PORT", "5432"),
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





def initialize_db(config: AppConfig) -> None:
    DATABASE.init(
        config.pg_db_name,
        user=config.pg_db_user,
        password=config.pg_db_password,
        host=config.pg_db_host,
        port=config.pg_db_port,
    )
