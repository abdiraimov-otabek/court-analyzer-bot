from __future__ import annotations

import logging
import sqlite3
import time
from datetime import datetime, timedelta

from src.domain.entities import AnalysisResult
from src.infrastructure.sqlite import SqliteConnection


class AnalysisCacheRepository:
    def __init__(
        self, connection: SqliteConnection, ttl_seconds: int = 24 * 60 * 60
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._connection = connection
        self._last_cleanup_at = 0.0
        self._ttl = timedelta(seconds=ttl_seconds)
        self._disabled = False
        self._logger = logging.getLogger("analysis_cache_repository")

    def get(self, cache_key: str, now: datetime) -> AnalysisResult | None:
        if self._disabled:
            return None
        t_now = time.time()
        if t_now - self._last_cleanup_at > 60:
            self._cleanup(now)
            self._last_cleanup_at = t_now
        try:
            with self._connection.connect() as conn:
                row = conn.execute(
                    "select summary, case_list, expires_at from analysis_cache where cache_key = ?",
                    (cache_key,),
                ).fetchone()
        except sqlite3.DatabaseError as exc:
            if self._disable_on_corruption(exc):
                return None
            raise
        if row is None:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at <= now:
            self.delete(cache_key)
            return None
        return AnalysisResult(summary=row["summary"], case_list=row["case_list"])

    def set(self, cache_key: str, result: AnalysisResult, now: datetime) -> None:
        if self._disabled:
            return
        expires_at = now + self._ttl
        try:
            with self._connection.connect() as conn:
                conn.execute(
                    """
                    insert into analysis_cache (cache_key, summary, case_list, created_at, expires_at)
                    values (?, ?, ?, ?, ?)
                    on conflict(cache_key) do update set
                        summary=excluded.summary,
                        case_list=excluded.case_list,
                        created_at=excluded.created_at,
                        expires_at=excluded.expires_at
                    """,
                    (
                        cache_key,
                        result.summary,
                        result.case_list,
                        now.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
                conn.commit()
        except sqlite3.DatabaseError as exc:
            if self._disable_on_corruption(exc):
                return
            raise

    def delete(self, cache_key: str) -> None:
        if self._disabled:
            return
        try:
            with self._connection.connect() as conn:
                conn.execute(
                    "delete from analysis_cache where cache_key = ?", (cache_key,)
                )
                conn.commit()
        except sqlite3.DatabaseError as exc:
            if self._disable_on_corruption(exc):
                return
            raise

    def _cleanup(self, now: datetime) -> None:
        if self._disabled:
            return
        try:
            with self._connection.connect() as conn:
                conn.execute(
                    "delete from analysis_cache where expires_at <= ?",
                    (now.isoformat(),),
                )
                conn.commit()
        except sqlite3.DatabaseError as exc:
            if self._disable_on_corruption(exc):
                return
            raise

    def _disable_on_corruption(self, exc: sqlite3.DatabaseError) -> bool:
        message = str(exc).lower()
        corruption_markers = (
            "database disk image is malformed",
            "malformed",
            "file is not a database",
        )
        if not any(marker in message for marker in corruption_markers):
            return False
        if not self._disabled:
            self._logger.warning(
                "analysis_cache.disabled_due_to_corruption",
                extra={"error": str(exc)},
            )
        self._disabled = True
        return True
