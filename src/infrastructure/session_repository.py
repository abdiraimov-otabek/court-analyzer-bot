from __future__ import annotations

import time

from src.infrastructure.sqlite import SqliteConnection


class AdminSessionRepository:
    def __init__(self, connection: SqliteConnection) -> None:
        self._connection = connection

    def create_session(self, session_id: str, ttl: int) -> None:
        expires_at = time.time() + ttl
        with self._connection.connect() as conn:
            conn.execute(
                "INSERT INTO admin_sessions (session_id, expires_at) VALUES (?, ?)",
                (session_id, expires_at),
            )

    def validate_session(self, session_id: str) -> bool:
        if not session_id:
            return False
        now = time.time()
        with self._connection.connect() as conn:
            row = conn.execute(
                "SELECT expires_at FROM admin_sessions WHERE session_id = ?",
                (session_id,),
            ).fetchone()

            if row is None:
                return False

            expires_at = row[0]
            if now > expires_at:
                conn.execute(
                    "DELETE FROM admin_sessions WHERE session_id = ?", (session_id,)
                )
                return False
            return True

    def delete_session(self, session_id: str) -> None:
        with self._connection.connect() as conn:
            conn.execute(
                "DELETE FROM admin_sessions WHERE session_id = ?", (session_id,)
            )
