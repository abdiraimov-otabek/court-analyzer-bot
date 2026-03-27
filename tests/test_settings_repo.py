from datetime import datetime

from src.domain.settings import Settings
from src.infrastructure.settings_repository import SettingsRepository
from src.infrastructure.sqlite import SqliteConnection


def test_settings_repository_roundtrip(tmp_path):
    db_path = tmp_path / "app.db"
    connection = SqliteConnection(str(db_path))
    repo = SettingsRepository(connection)

    default_settings = repo.get()
    assert default_settings.max_cases == 500
    assert default_settings.fetch_concurrency_min == 6
    assert default_settings.fetch_concurrency_max == 10
    assert default_settings.max_llm_calls_per_request == 50
    assert default_settings.max_analysis_text_length == 50_000
    assert default_settings.allow_all_users is False
    assert default_settings.unknown_outcome_threshold_percent == 35
    assert default_settings.court_mismatch_threshold_percent == 20
    assert default_settings.min_known_outcomes == 5
    assert default_settings.send_partial_file_on_quality_fail is True
    assert default_settings.pdf_required_for_article_queries is True
    assert default_settings.enable_ocr_fallback is True
    assert default_settings.candidate_pool_multiplier == 4
    assert default_settings.max_pdf_pages_per_case == 20
    assert default_settings.pdf_fetch_timeout_seconds == 45
    assert default_settings.allow_law_inference is True

    updated = Settings(
        max_cases=200,
        max_documents_per_case=7,
        max_pages=15,
        max_llm_calls_per_request=15,
        max_analysis_text_length=20_000,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        unknown_outcome_threshold_percent=10,
        court_mismatch_threshold_percent=25,
        min_known_outcomes=60,
        send_partial_file_on_quality_fail=False,
        pdf_required_for_article_queries=False,
        enable_ocr_fallback=False,
        candidate_pool_multiplier=3,
        max_pdf_pages_per_case=25,
        pdf_fetch_timeout_seconds=90,
        allow_law_inference=False,
        analysis_prompt="test",
        updated_at=datetime(2026, 2, 11, 12, 0, 0),
    )
    repo.save(updated)

    loaded = repo.get()
    assert loaded.max_cases == 200
    assert loaded.max_documents_per_case == 7
    assert loaded.max_pages == 15
    assert loaded.max_llm_calls_per_request == 15
    assert loaded.max_analysis_text_length == 20_000
    assert loaded.fetch_concurrency_min == 6
    assert loaded.fetch_concurrency_max == 10
    assert loaded.slow_alert_minutes == 5
    assert loaded.details_cache_ttl_seconds == 24 * 60 * 60
    assert loaded.allow_all_users is False
    assert loaded.unknown_outcome_threshold_percent == 10
    assert loaded.court_mismatch_threshold_percent == 25
    assert loaded.min_known_outcomes == 60
    assert loaded.send_partial_file_on_quality_fail is False
    assert loaded.pdf_required_for_article_queries is False
    assert loaded.enable_ocr_fallback is False
    assert loaded.candidate_pool_multiplier == 3
    assert loaded.max_pdf_pages_per_case == 25
    assert loaded.pdf_fetch_timeout_seconds == 90
    assert loaded.allow_law_inference is False
    assert loaded.analysis_prompt == "test"
