from __future__ import annotations

from datetime import datetime

from src.domain.settings import Settings
from src.infrastructure.settings_repository import SettingsRepository


class SettingsService:
    def __init__(self, repository: SettingsRepository) -> None:
        self._repository = repository

    def get_settings(self) -> Settings:
        return self._repository.get()

    def update_settings(
        self,
        max_cases: int,
        max_documents_per_case: int,
        max_pages: int,
        fetch_concurrency_min: int,
        fetch_concurrency_max: int,
        slow_alert_minutes: int,
        details_cache_ttl_seconds: int,
        allow_all_users: bool,
        analysis_prompt: str,
        unknown_outcome_threshold_percent: int,
        court_mismatch_threshold_percent: int,
        min_known_outcomes: int,
        send_partial_file_on_quality_fail: bool,
    ) -> Settings:
        if max_cases < 5 or max_cases > 50:
            raise ValueError("max_cases must be between 5 and 5000")
        if max_documents_per_case <= 0:
            raise ValueError("max_documents_per_case must be positive")
        if max_pages <= 0:
            raise ValueError("max_pages must be positive")
        if fetch_concurrency_min < 1:
            raise ValueError("fetch_concurrency_min must be >= 1")
        if fetch_concurrency_max < fetch_concurrency_min:
            raise ValueError("fetch_concurrency_max must be >= fetch_concurrency_min")
        if slow_alert_minutes < 1:
            raise ValueError("slow_alert_minutes must be >= 1")
        if details_cache_ttl_seconds < 60:
            raise ValueError("details_cache_ttl_seconds must be >= 60")
        if not analysis_prompt.strip():
            raise ValueError("analysis_prompt cannot be empty")
        if unknown_outcome_threshold_percent < 1 or unknown_outcome_threshold_percent > 100:
            raise ValueError("unknown_outcome_threshold_percent must be between 1 and 100")
        if court_mismatch_threshold_percent < 1 or court_mismatch_threshold_percent > 100:
            raise ValueError("court_mismatch_threshold_percent must be between 1 and 100")
        if min_known_outcomes < 5:
            raise ValueError("min_known_outcomes must be >= 5")
        if min_known_outcomes > max_cases:
            raise ValueError("min_known_outcomes must be <= max_cases")
        settings = Settings(
            max_cases=max_cases,
            max_documents_per_case=max_documents_per_case,
            max_pages=max_pages,
            fetch_concurrency_min=fetch_concurrency_min,
            fetch_concurrency_max=fetch_concurrency_max,
            slow_alert_minutes=slow_alert_minutes,
            details_cache_ttl_seconds=details_cache_ttl_seconds,
            analysis_prompt=analysis_prompt,
            updated_at=datetime.now(),
            allow_all_users=allow_all_users,
            unknown_outcome_threshold_percent=unknown_outcome_threshold_percent,
            court_mismatch_threshold_percent=court_mismatch_threshold_percent,
            min_known_outcomes=min_known_outcomes,
            send_partial_file_on_quality_fail=send_partial_file_on_quality_fail,
        )
        self._repository.save(settings)
        return settings
