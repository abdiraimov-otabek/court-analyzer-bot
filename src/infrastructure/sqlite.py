from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Generator


class SqliteConnection:
    def __init__(self, db_path: str) -> None:
        path = Path(db_path).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        self._db_path = str(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(self._db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists settings (
                    id integer primary key,
                    max_cases integer not null,
                    max_documents_per_case integer not null,
                    max_pages integer not null,
                    fetch_concurrency_min integer not null default 6,
                    fetch_concurrency_max integer not null default 10,
                    slow_alert_minutes integer not null default 5,
                    details_cache_ttl_seconds integer not null default 86400,
                    allow_all_users integer not null default 0,
                    unknown_outcome_threshold_percent integer not null default 15,
                    court_mismatch_threshold_percent integer not null default 20,
                    min_known_outcomes integer not null default 50,
                    send_partial_file_on_quality_fail integer not null default 1,
                    analysis_prompt text not null,
                    updated_at text not null
                );
                create table if not exists allowed_users (
                    telegram_id text primary key,
                    created_at text not null
                );
                create table if not exists logs (
                    id integer primary key,
                    hashed_user_id text not null,
                    query_text text not null,
                    result_summary text not null,
                    created_at text not null
                );
                create table if not exists analysis_cache (
                    cache_key text primary key,
                    summary text not null,
                    case_list text not null,
                    created_at text not null,
                    expires_at text not null
                );
                create table if not exists case_details_cache (
                    case_id text primary key,
                    payload text not null,
                    created_at text not null,
                    expires_at text not null
                );
                create table if not exists active_requests (
                    user_id text primary key,
                    query_text text not null,
                    phase text not null default 'counting',
                    total_cases integer not null default 0,
                    started_at text not null,
                    updated_at text not null
                );
                create index if not exists idx_case_details_cache_expires_at on case_details_cache(expires_at);
                create index if not exists idx_case_details_cache_case_id on case_details_cache(case_id);
                """
            )
            self._ensure_settings_columns(conn)

    def _ensure_settings_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("pragma table_info(settings)").fetchall()
        }
        if "fetch_concurrency_min" not in columns:
            conn.execute("alter table settings add column fetch_concurrency_min integer not null default 6")
        if "fetch_concurrency_max" not in columns:
            conn.execute("alter table settings add column fetch_concurrency_max integer not null default 10")
        if "slow_alert_minutes" not in columns:
            conn.execute("alter table settings add column slow_alert_minutes integer not null default 5")
        if "details_cache_ttl_seconds" not in columns:
            conn.execute("alter table settings add column details_cache_ttl_seconds integer not null default 86400")
        if "allow_all_users" not in columns:
            conn.execute("alter table settings add column allow_all_users integer not null default 0")
        if "unknown_outcome_threshold_percent" not in columns:
            conn.execute("alter table settings add column unknown_outcome_threshold_percent integer not null default 15")
        if "court_mismatch_threshold_percent" not in columns:
            conn.execute("alter table settings add column court_mismatch_threshold_percent integer not null default 20")
        if "min_known_outcomes" not in columns:
            conn.execute("alter table settings add column min_known_outcomes integer not null default 50")
        if "send_partial_file_on_quality_fail" not in columns:
            conn.execute("alter table settings add column send_partial_file_on_quality_fail integer not null default 1")
