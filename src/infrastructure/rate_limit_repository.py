from __future__ import annotations

import time

from src.infrastructure.sqlite import SqliteConnection


class LoginRateLimitRepository:
    def __init__(self, connection: SqliteConnection) -> None:
        self._connection = connection

    def add_attempt(self, ip_address: str) -> None:
        now = time.time()
        with self._connection.connect() as conn:
            conn.execute(
                "INSERT INTO login_attempts (ip_address, attempt_at) VALUES (?, ?)",
                (ip_address, now),
            )

    def count_attempts(self, ip_address: str, window_seconds: int) -> int:
        since = time.time() - window_seconds
        with self._connection.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM login_attempts WHERE ip_address = ? AND attempt_at > ?",
                (ip_address, since),
            ).fetchone()
            return row[0] if row else 0

    def cleanup(self) -> None:
        # Generic cleanup of old attempts to prevent table bloat
        expired = time.time() - (24 * 3600)  # 24 hours
        with self._connection.connect() as conn:
            conn.execute("DELETE FROM login_attempts WHERE attempt_at < ?", (expired,))
