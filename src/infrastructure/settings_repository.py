from __future__ import annotations

from datetime import datetime

from src.domain.settings import Settings, default_settings
from src.infrastructure.sqlite import SqliteConnection


class SettingsRepository:
    def __init__(self, connection: SqliteConnection) -> None:
        self._connection = connection

    def get(self) -> Settings:
        with self._connection.connect() as conn:
            row = conn.execute("select * from settings limit 1").fetchone()
        if row is None:
            settings = default_settings(datetime.now())
            self.save(settings)
            return settings
        return Settings(
            max_cases=row["max_cases"],
            max_documents_per_case=row["max_documents_per_case"],
            max_pages=row["max_pages"],
            max_llm_calls_per_request=row["max_llm_calls_per_request"],
            max_analysis_text_length=row["max_analysis_text_length"],
            fetch_concurrency_min=row["fetch_concurrency_min"],
            fetch_concurrency_max=row["fetch_concurrency_max"],
            slow_alert_minutes=row["slow_alert_minutes"],
            details_cache_ttl_seconds=row["details_cache_ttl_seconds"],
            analysis_prompt=row["analysis_prompt"],
            updated_at=datetime.fromisoformat(row["updated_at"]),
            allow_all_users=bool(row["allow_all_users"]),
            unknown_outcome_threshold_percent=row["unknown_outcome_threshold_percent"],
            court_mismatch_threshold_percent=row["court_mismatch_threshold_percent"],
            min_known_outcomes=row["min_known_outcomes"],
            send_partial_file_on_quality_fail=bool(row["send_partial_file_on_quality_fail"]),
            pdf_required_for_article_queries=bool(row["pdf_required_for_article_queries"]),
            enable_ocr_fallback=bool(row["enable_ocr_fallback"]),
            candidate_pool_multiplier=row["candidate_pool_multiplier"],
            max_pdf_pages_per_case=row["max_pdf_pages_per_case"],
            pdf_fetch_timeout_seconds=row["pdf_fetch_timeout_seconds"],
            allow_law_inference=bool(row["allow_law_inference"]),
        )

    def save(self, settings: Settings) -> None:
        with self._connection.connect() as conn:
            conn.execute("delete from settings")
            conn.execute(
                """
                insert into settings (
                    max_cases,
                    max_documents_per_case,
                    max_pages,
                    max_llm_calls_per_request,
                    max_analysis_text_length,
                    fetch_concurrency_min,
                    fetch_concurrency_max,
                    slow_alert_minutes,
                    details_cache_ttl_seconds,
                    allow_all_users,
                    unknown_outcome_threshold_percent,
                    court_mismatch_threshold_percent,
                    min_known_outcomes,
                    send_partial_file_on_quality_fail,
                    pdf_required_for_article_queries,
                    enable_ocr_fallback,
                    candidate_pool_multiplier,
                    max_pdf_pages_per_case,
                    pdf_fetch_timeout_seconds,
                    allow_law_inference,
                    analysis_prompt,
                    updated_at
                )
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    settings.max_cases,
                    settings.max_documents_per_case,
                    settings.max_pages,
                    settings.max_llm_calls_per_request,
                    settings.max_analysis_text_length,
                    settings.fetch_concurrency_min,
                    settings.fetch_concurrency_max,
                    settings.slow_alert_minutes,
                    settings.details_cache_ttl_seconds,
                    1 if settings.allow_all_users else 0,
                    settings.unknown_outcome_threshold_percent,
                    settings.court_mismatch_threshold_percent,
                    settings.min_known_outcomes,
                    1 if settings.send_partial_file_on_quality_fail else 0,
                    1 if settings.pdf_required_for_article_queries else 0,
                    1 if settings.enable_ocr_fallback else 0,
                    settings.candidate_pool_multiplier,
                    settings.max_pdf_pages_per_case,
                    settings.pdf_fetch_timeout_seconds,
                    1 if settings.allow_law_inference else 0,
                    settings.analysis_prompt,
                    settings.updated_at.isoformat(),
                ),
            )
