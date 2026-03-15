from __future__ import annotations

from datetime import datetime

from src.infrastructure.sqlite import SqliteConnection


class AllowedUsersRepository:
    def __init__(self, connection: SqliteConnection) -> None:
        self._connection = connection

    def list_all(self) -> list[str]:
        with self._connection.connect() as conn:
            rows = conn.execute("select telegram_id from allowed_users order by telegram_id").fetchall()
        return [row["telegram_id"] for row in rows]

    def grant(self, telegram_id: str) -> None:
        with self._connection.connect() as conn:
            conn.execute(
                "insert or ignore into allowed_users (telegram_id, created_at) values (?, ?)",
                (telegram_id, datetime.now().isoformat()),
            )

    def revoke(self, telegram_id: str) -> None:
        with self._connection.connect() as conn:
            conn.execute("delete from allowed_users where telegram_id = ?", (telegram_id,))

    def is_allowed(self, telegram_id: str) -> bool:
        with self._connection.connect() as conn:
            row = conn.execute("select telegram_id from allowed_users where telegram_id = ?", (telegram_id,)).fetchone()
        return row is not None
