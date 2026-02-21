from __future__ import annotations

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

    def get(self, cache_key: str, now: datetime) -> AnalysisResult | None:
        t_now = time.time()
        if t_now - self._last_cleanup_at > 60:
            self._cleanup(now)
            self._last_cleanup_at = t_now
        with self._connection.connect() as conn:
            row = conn.execute(
                "select summary, case_list, expires_at from analysis_cache where cache_key = ?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        expires_at = datetime.fromisoformat(row["expires_at"])
        if expires_at <= now:
            self.delete(cache_key)
            return None
        return AnalysisResult(summary=row["summary"], case_list=row["case_list"])

    def set(self, cache_key: str, result: AnalysisResult, now: datetime) -> None:
        expires_at = now + self._ttl
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

    def delete(self, cache_key: str) -> None:
        with self._connection.connect() as conn:
            conn.execute("delete from analysis_cache where cache_key = ?", (cache_key,))
            conn.commit()

    def _cleanup(self, now: datetime) -> None:
        with self._connection.connect() as conn:
            conn.execute(
                "delete from analysis_cache where expires_at <= ?", (now.isoformat(),)
            )
            conn.commit()
