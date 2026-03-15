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
    max_llm_calls_per_request: int = 50
    max_analysis_text_length: int = 50_000
    allow_all_users: bool = False
    unknown_outcome_threshold_percent: int = 35
    court_mismatch_threshold_percent: int = 20
    min_known_outcomes: int = 5
    send_partial_file_on_quality_fail: bool = True
    pdf_required_for_article_queries: bool = True
    enable_ocr_fallback: bool = True
    candidate_pool_multiplier: int = 4
    max_pdf_pages_per_case: int = 20
    pdf_fetch_timeout_seconds: int = 45
    allow_law_inference: bool = True


def default_settings(now: datetime) -> Settings:
    return Settings(
        max_cases=50,
        max_documents_per_case=5,
        max_pages=80,
        max_llm_calls_per_request=50,
        max_analysis_text_length=50_000,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        analysis_prompt="Сформируй статистику по результатам рассмотрения...",
        updated_at=now,
        allow_all_users=False,
        unknown_outcome_threshold_percent=35,
        court_mismatch_threshold_percent=20,
        min_known_outcomes=5,
        send_partial_file_on_quality_fail=True,
        pdf_required_for_article_queries=True,
        enable_ocr_fallback=True,
        candidate_pool_multiplier=4,
        max_pdf_pages_per_case=20,
        pdf_fetch_timeout_seconds=45,
        allow_law_inference=True,
    )
