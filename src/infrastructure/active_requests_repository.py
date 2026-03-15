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

    def upsert(
        self,
        user_id: str,
        request_id: str,
        query_text: str,
        phase: str,
        total_cases: int,
    ) -> None:
        try:
            with self._connection.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO active_requests (user_id, request_id, query_text, phase, total_cases, cancelled, started_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 0, strftime('%s','now'), strftime('%s','now'))
                    ON CONFLICT(user_id) DO UPDATE SET
                        request_id=excluded.request_id,
                        query_text=excluded.query_text,
                        phase=excluded.phase,
                        total_cases=excluded.total_cases,
                        updated_at=excluded.updated_at
                    """,
                    (user_id, request_id, query_text, phase, total_cases),
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

    def set_cancelled(self, user_id: str, cancelled: bool) -> None:
        try:
            with self._connection.connect() as conn:
                conn.execute(
                    "UPDATE active_requests SET cancelled=?, updated_at=strftime('%s','now') WHERE user_id=?",
                    (1 if cancelled else 0, user_id),
                )
        except Exception as exc:
            logger.warning("active_requests set_cancelled failed: %s", exc)

    def is_cancelled(self, user_id: str) -> bool:
        try:
            with self._connection.connect() as conn:
                row = conn.execute(
                    "SELECT cancelled FROM active_requests WHERE user_id=?",
                    (user_id,),
                ).fetchone()
                return bool(row[0]) if row else False
        except Exception as exc:
            logger.warning("active_requests is_cancelled failed: %s", exc)
            return False

    def get(self, user_id: str) -> dict[str, Any] | None:
        try:
            with self._connection.connect() as conn:
                row = conn.execute(
                    """
                    SELECT user_id, request_id, query_text, phase, total_cases, cancelled, started_at, updated_at
                    FROM active_requests
                    WHERE user_id=?
                    """,
                    (user_id,),
                ).fetchone()
                if row is None:
                    return None
                return {
                    "user_id": row["user_id"],
                    "request_id": row["request_id"],
                    "query_text": row["query_text"],
                    "phase": row["phase"],
                    "total_cases": row["total_cases"],
                    "cancelled": bool(row["cancelled"]),
                    "started_at": row["started_at"],
                    "updated_at": row["updated_at"],
                }
        except Exception as exc:
            logger.warning("active_requests get failed: %s", exc)
            return None

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
                    """
                    SELECT user_id, request_id, query_text, phase, total_cases, cancelled, started_at, updated_at
                    FROM active_requests
                    """
                ).fetchall()
                return [
                    {
                        "user_id": row["user_id"],
                        "request_id": row["request_id"],
                        "query_text": row["query_text"],
                        "phase": row["phase"],
                        "total_cases": row["total_cases"],
                        "cancelled": bool(row["cancelled"]),
                        "started_at": row["started_at"],
                        "updated_at": row["updated_at"],
                    }
                    for row in rows
                ]
        except Exception as exc:
            logger.warning("active_requests list_all failed: %s", exc)
            return []
