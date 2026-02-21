from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class Settings:
    max_cases: int
    max_documents_per_case: int
    max_pages: int
    fetch_concurrency_min: int
    fetch_concurrency_max: int
    slow_alert_minutes: int
    details_cache_ttl_seconds: int
    analysis_prompt: str
    updated_at: datetime
    allow_all_users: bool = False
    unknown_outcome_threshold_percent: int = 35
    court_mismatch_threshold_percent: int = 20
    min_known_outcomes: int = 50
    send_partial_file_on_quality_fail: bool = True


def default_settings(now: datetime) -> Settings:
    return Settings(
        max_cases=2000,
        max_documents_per_case=5,
        max_pages=80,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        analysis_prompt="Сформируй статистику по результатам рассмотрения...",
        updated_at=now,
        allow_all_users=False,
        unknown_outcome_threshold_percent=35,
        court_mismatch_threshold_percent=20,
        min_known_outcomes=50,
        send_partial_file_on_quality_fail=True,
    )
