from __future__ import annotations

from datetime import datetime

from src.infrastructure.sqlite import SqliteConnection


class LogRepository:
    def __init__(self, connection: SqliteConnection) -> None:
        self._connection = connection

    def append(self, hashed_user_id: str, query_text: str, result_summary: str) -> None:
        with self._connection.connect() as conn:
            conn.execute(
                """
                insert into logs (hashed_user_id, query_text, result_summary, created_at)
                values (?, ?, ?, ?)
                """,
                (hashed_user_id, query_text, result_summary, datetime.now().isoformat()),
            )
