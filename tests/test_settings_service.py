import pytest

from src.infrastructure.settings_repository import SettingsRepository
from src.infrastructure.sqlite import SqliteConnection
from src.services.settings_service import SettingsService


def test_settings_service_updates_valid_values(tmp_path):
    repo = SettingsRepository(SqliteConnection(str(tmp_path / "app.db")))
    service = SettingsService(repo)

    updated = service.update_settings(
        max_cases=400,
        max_documents_per_case=7,
        max_pages=30,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        allow_all_users=True,
        analysis_prompt="prompt",
        unknown_outcome_threshold_percent=15,
        court_mismatch_threshold_percent=20,
        min_known_outcomes=50,
        send_partial_file_on_quality_fail=True,
    )

    assert updated.max_cases == 400
    assert updated.max_documents_per_case == 7
    assert updated.max_pages == 30
    assert updated.fetch_concurrency_min == 6
    assert updated.fetch_concurrency_max == 10
    assert updated.slow_alert_minutes == 5
    assert updated.details_cache_ttl_seconds == 24 * 60 * 60
    assert updated.allow_all_users is True
    assert updated.analysis_prompt == "prompt"
    assert updated.unknown_outcome_threshold_percent == 15
    assert updated.court_mismatch_threshold_percent == 20
    assert updated.min_known_outcomes == 50
    assert updated.send_partial_file_on_quality_fail is True


@pytest.mark.parametrize(
    "payload",
    [
        {"max_cases": 4, "max_documents_per_case": 5, "max_pages": 20, "analysis_prompt": "x"},
        {"max_cases": 501, "max_documents_per_case": 5, "max_pages": 20, "analysis_prompt": "x"},
        {"max_cases": 100, "max_documents_per_case": 0, "max_pages": 20, "analysis_prompt": "x"},
        {"max_cases": 100, "max_documents_per_case": 5, "max_pages": 0, "analysis_prompt": "x"},
        {
            "max_cases": 100,
            "max_documents_per_case": 5,
            "max_pages": 20,
            "fetch_concurrency_min": 0,
            "fetch_concurrency_max": 10,
            "slow_alert_minutes": 5,
            "details_cache_ttl_seconds": 86400,
            "analysis_prompt": "x",
        },
        {
            "max_cases": 100,
            "max_documents_per_case": 5,
            "max_pages": 20,
            "fetch_concurrency_min": 6,
            "fetch_concurrency_max": 5,
            "slow_alert_minutes": 5,
            "details_cache_ttl_seconds": 86400,
            "analysis_prompt": "x",
        },
        {
            "max_cases": 100,
            "max_documents_per_case": 5,
            "max_pages": 20,
            "fetch_concurrency_min": 6,
            "fetch_concurrency_max": 10,
            "slow_alert_minutes": 0,
            "details_cache_ttl_seconds": 86400,
            "analysis_prompt": "x",
        },
        {
            "max_cases": 100,
            "max_documents_per_case": 5,
            "max_pages": 20,
            "fetch_concurrency_min": 6,
            "fetch_concurrency_max": 10,
            "slow_alert_minutes": 5,
            "details_cache_ttl_seconds": 59,
            "analysis_prompt": "x",
        },
        {"max_cases": 100, "max_documents_per_case": 5, "max_pages": 20, "analysis_prompt": "   "},
        {
            "max_cases": 100,
            "max_documents_per_case": 5,
            "max_pages": 20,
            "fetch_concurrency_min": 6,
            "fetch_concurrency_max": 10,
            "slow_alert_minutes": 5,
            "details_cache_ttl_seconds": 86400,
            "unknown_outcome_threshold_percent": 0,
            "analysis_prompt": "x",
        },
        {
            "max_cases": 100,
            "max_documents_per_case": 5,
            "max_pages": 20,
            "fetch_concurrency_min": 6,
            "fetch_concurrency_max": 10,
            "slow_alert_minutes": 5,
            "details_cache_ttl_seconds": 86400,
            "court_mismatch_threshold_percent": 101,
            "analysis_prompt": "x",
        },
        {
            "max_cases": 100,
            "max_documents_per_case": 5,
            "max_pages": 20,
            "fetch_concurrency_min": 6,
            "fetch_concurrency_max": 10,
            "slow_alert_minutes": 5,
            "details_cache_ttl_seconds": 86400,
            "min_known_outcomes": 101,
            "analysis_prompt": "x",
        },
    ],
)
def test_settings_service_rejects_invalid_values(tmp_path, payload):
    repo = SettingsRepository(SqliteConnection(str(tmp_path / "app.db")))
    service = SettingsService(repo)

    with pytest.raises(ValueError):
        service.update_settings(
            max_cases=payload["max_cases"],
            max_documents_per_case=payload["max_documents_per_case"],
            max_pages=payload["max_pages"],
            fetch_concurrency_min=payload.get("fetch_concurrency_min", 6),
            fetch_concurrency_max=payload.get("fetch_concurrency_max", 10),
            slow_alert_minutes=payload.get("slow_alert_minutes", 5),
            details_cache_ttl_seconds=payload.get("details_cache_ttl_seconds", 24 * 60 * 60),
            allow_all_users=False,
            analysis_prompt=payload["analysis_prompt"],
            unknown_outcome_threshold_percent=payload.get("unknown_outcome_threshold_percent", 15),
            court_mismatch_threshold_percent=payload.get("court_mismatch_threshold_percent", 20),
            min_known_outcomes=payload.get("min_known_outcomes", 50),
            send_partial_file_on_quality_fail=payload.get("send_partial_file_on_quality_fail", True),
        )
