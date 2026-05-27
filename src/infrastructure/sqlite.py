import sqlite3
import threading
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
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._ensure_schema()

    @contextmanager
    def connect(self) -> Generator[sqlite3.Connection, None, None]:
        if self._conn is None:
            with self._lock:  # Ensure only one thread initializes
                if self._conn is None:
                    self._conn = sqlite3.connect(
                        self._db_path, timeout=30, check_same_thread=False
                    )
                    self._conn.row_factory = sqlite3.Row
                    self._conn.execute("PRAGMA journal_mode=WAL")
                    self._conn.execute("PRAGMA synchronous=NORMAL")

        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            if self._conn:
                self._conn.rollback()
            raise

    def _ensure_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                create table if not exists settings (
                    id integer primary key,
                    max_cases integer not null,
                    max_documents_per_case integer not null,
                    max_pages integer not null,
                    max_llm_calls_per_request integer not null default 50,
                    max_analysis_text_length integer not null default 50000,
                    fetch_concurrency_min integer not null default 6,
                    fetch_concurrency_max integer not null default 10,
                    slow_alert_minutes integer not null default 5,
                    details_cache_ttl_seconds integer not null default 86400,
                    allow_all_users integer not null default 0,
                    unknown_outcome_threshold_percent integer not null default 15,
                    court_mismatch_threshold_percent integer not null default 20,
                    min_known_outcomes integer not null default 50,
                    send_partial_file_on_quality_fail integer not null default 1,
                    pdf_required_for_article_queries integer not null default 1,
                    enable_ocr_fallback integer not null default 1,
                    candidate_pool_multiplier integer not null default 4,
                    max_pdf_pages_per_case integer not null default 20,
                    pdf_fetch_timeout_seconds integer not null default 45,
                    allow_law_inference integer not null default 1,
                    llm_model text not null default 'anthropic/claude-3.5-sonnet',
                    fast_llm_model text not null default 'google/gemini-2.0-flash-001',
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
                create table if not exists admin_sessions (
                    session_id text primary key,
                    expires_at real not null
                );
                create table if not exists login_attempts (
                    ip_address text not null,
                    attempt_at real not null
                );
                create index if not exists idx_login_attempts_ip on login_attempts(ip_address);
                create table if not exists active_requests (
                    user_id text primary key,
                    request_id text not null default '',
                    query_text text not null,
                    phase text not null default 'counting',
                    total_cases integer not null default 0,
                    cancelled integer not null default 0,
                    started_at text not null,
                    updated_at text not null
                );
                create index if not exists idx_case_details_cache_case_id on case_details_cache(case_id);

                -- Cleanup stale active requests (older than 1 hour) on startup
                delete from active_requests where updated_at < strftime('%s', 'now', '-1 hour');
                -- Cleanup stale sessions
                delete from admin_sessions where expires_at < strftime('%s', 'now');
                -- Cleanup stale login attempts (older than 15 mins)
                delete from login_attempts where attempt_at < strftime('%s', 'now', '-15 minutes');
                """
            )
            self._ensure_settings_columns(conn)
            self._ensure_active_requests_columns(conn)

    def _ensure_settings_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("pragma table_info(settings)").fetchall()
        }
        if "fetch_concurrency_min" not in columns:
            conn.execute(
                "alter table settings add column fetch_concurrency_min integer not null default 6"
            )
        if "max_llm_calls_per_request" not in columns:
            conn.execute(
                "alter table settings add column max_llm_calls_per_request integer not null default 50"
            )
        if "max_analysis_text_length" not in columns:
            conn.execute(
                "alter table settings add column max_analysis_text_length integer not null default 50000"
            )
        if "fetch_concurrency_max" not in columns:
            conn.execute(
                "alter table settings add column fetch_concurrency_max integer not null default 10"
            )
        if "slow_alert_minutes" not in columns:
            conn.execute(
                "alter table settings add column slow_alert_minutes integer not null default 5"
            )
        if "details_cache_ttl_seconds" not in columns:
            conn.execute(
                "alter table settings add column details_cache_ttl_seconds integer not null default 86400"
            )
        if "allow_all_users" not in columns:
            conn.execute(
                "alter table settings add column allow_all_users integer not null default 0"
            )
        if "unknown_outcome_threshold_percent" not in columns:
            conn.execute(
                "alter table settings add column unknown_outcome_threshold_percent integer not null default 15"
            )
        if "court_mismatch_threshold_percent" not in columns:
            conn.execute(
                "alter table settings add column court_mismatch_threshold_percent integer not null default 20"
            )
        if "min_known_outcomes" not in columns:
            conn.execute(
                "alter table settings add column min_known_outcomes integer not null default 50"
            )
        if "send_partial_file_on_quality_fail" not in columns:
            conn.execute(
                "alter table settings add column send_partial_file_on_quality_fail integer not null default 1"
            )
        if "pdf_required_for_article_queries" not in columns:
            conn.execute(
                "alter table settings add column pdf_required_for_article_queries integer not null default 1"
            )
        if "enable_ocr_fallback" not in columns:
            conn.execute(
                "alter table settings add column enable_ocr_fallback integer not null default 1"
            )
        if "candidate_pool_multiplier" not in columns:
            conn.execute(
                "alter table settings add column candidate_pool_multiplier integer not null default 4"
            )
        if "max_pdf_pages_per_case" not in columns:
            conn.execute(
                "alter table settings add column max_pdf_pages_per_case integer not null default 20"
            )
        if "pdf_fetch_timeout_seconds" not in columns:
            conn.execute(
                "alter table settings add column pdf_fetch_timeout_seconds integer not null default 45"
            )
        if "allow_law_inference" not in columns:
            conn.execute(
                "alter table settings add column allow_law_inference integer not null default 1"
            )
        if "llm_model" not in columns:
            conn.execute(
                "alter table settings add column llm_model text not null default 'anthropic/claude-3.5-sonnet'"
            )
        if "fast_llm_model" not in columns:
            conn.execute(
                "alter table settings add column fast_llm_model text not null default 'google/gemini-2.0-flash-001'"
            )

    def _ensure_active_requests_columns(self, conn: sqlite3.Connection) -> None:
        columns = {
            row["name"]
            for row in conn.execute("pragma table_info(active_requests)").fetchall()
        }
        if "request_id" not in columns:
            conn.execute(
                "alter table active_requests add column request_id text not null default ''"
            )
        if "cancelled" not in columns:
            conn.execute(
                "alter table active_requests add column cancelled integer not null default 0"
            )
