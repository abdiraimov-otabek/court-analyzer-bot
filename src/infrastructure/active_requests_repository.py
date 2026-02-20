from __future__ import annotations

import logging
from typing import Any

from src.infrastructure.sqlite import SqliteConnection


logger = logging.getLogger("active_requests_repository")


class ActiveRequestsRepository:
    """Persists active request state to SQLite for cross-process visibility.

    The bot process writes phase/status here; the admin API process reads it.
    Counter updates (attempted/successful/retry) are intentionally NOT written
    to avoid excessive DB writes on every case fetch.
    """

    def __init__(self, connection: SqliteConnection) -> None:
        self._connection = connection

    def upsert(self, user_id: str, query_text: str, phase: str, total_cases: int) -> None:
        try:
            with self._connection.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO active_requests (user_id, query_text, phase, total_cases, started_at, updated_at)
                    VALUES (?, ?, ?, ?, strftime('%s','now'), strftime('%s','now'))
                    ON CONFLICT(user_id) DO UPDATE SET
                        query_text=excluded.query_text,
                        phase=excluded.phase,
                        total_cases=excluded.total_cases,
                        updated_at=excluded.updated_at
                    """,
                    (user_id, query_text, phase, total_cases),
                )
        except Exception as exc:
            logger.warning("active_requests upsert failed: %s", exc)

    def update_phase(self, user_id: str, phase: str) -> None:
        try:
            with self._connection.connect() as conn:
                conn.execute(
                    "UPDATE active_requests SET phase=?, updated_at=strftime('%s','now') WHERE user_id=?",
                    (phase, user_id),
                )
        except Exception as exc:
            logger.warning("active_requests update_phase failed: %s", exc)

    def delete(self, user_id: str) -> None:
        try:
            with self._connection.connect() as conn:
                conn.execute("DELETE FROM active_requests WHERE user_id=?", (user_id,))
        except Exception as exc:
            logger.warning("active_requests delete failed: %s", exc)

    def list_all(self) -> list[dict[str, Any]]:
        try:
            with self._connection.connect() as conn:
                rows = conn.execute(
                    "SELECT user_id, query_text, phase, total_cases, updated_at FROM active_requests"
                ).fetchall()
                return [
                    {
                        "user_id": row[0],
                        "query_text": row[1],
                        "phase": row[2],
                        "total_cases": row[3],
                        "updated_at": row[4],
                    }
                    for row in rows
                ]
        except Exception as exc:
            logger.warning("active_requests list_all failed: %s", exc)
            return []
