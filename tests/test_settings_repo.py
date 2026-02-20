from datetime import datetime

from src.infrastructure.sqlite import SqliteConnection
from src.infrastructure.settings_repository import SettingsRepository
from src.domain.settings import Settings


def test_settings_repository_roundtrip(tmp_path):
    db_path = tmp_path / "app.db"
    connection = SqliteConnection(str(db_path))
    repo = SettingsRepository(connection)

    default_settings = repo.get()
    assert default_settings.max_cases == 500
    assert default_settings.fetch_concurrency_min == 6
    assert default_settings.fetch_concurrency_max == 10
    assert default_settings.allow_all_users is False
    assert default_settings.unknown_outcome_threshold_percent == 35
    assert default_settings.court_mismatch_threshold_percent == 20
    assert default_settings.min_known_outcomes == 50
    assert default_settings.send_partial_file_on_quality_fail is True

    updated = Settings(
        max_cases=200,
        max_documents_per_case=7,
        max_pages=15,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        unknown_outcome_threshold_percent=10,
        court_mismatch_threshold_percent=25,
        min_known_outcomes=60,
        send_partial_file_on_quality_fail=False,
        analysis_prompt="test",
        updated_at=datetime(2026, 2, 11, 12, 0, 0),
    )
    repo.save(updated)

    loaded = repo.get()
    assert loaded.max_cases == 200
    assert loaded.max_documents_per_case == 7
    assert loaded.max_pages == 15
    assert loaded.fetch_concurrency_min == 6
    assert loaded.fetch_concurrency_max == 10
    assert loaded.slow_alert_minutes == 5
    assert loaded.details_cache_ttl_seconds == 24 * 60 * 60
    assert loaded.allow_all_users is False
    assert loaded.unknown_outcome_threshold_percent == 10
    assert loaded.court_mismatch_threshold_percent == 25
    assert loaded.min_known_outcomes == 60
    assert loaded.send_partial_file_on_quality_fail is False
    assert loaded.analysis_prompt == "test"
