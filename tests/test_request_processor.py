import asyncio
import sqlite3
from datetime import date, datetime

import pytest

from src.domain.analysis import AnalysisService
from src.domain.entities import CaseDecision, CaseOutcome
from src.domain.settings import Settings
from src.domain.value_objects import UserId
from src.infrastructure.cache_repository import AnalysisCacheRepository
from src.infrastructure.sqlite import SqliteConnection
from src.services.active_requests import ActiveRequestRegistry
from src.services.hashing import HashingService
from src.services.kad_client import FetchDecisionsResult, FetchStats, KadClient
from src.services.request_processor import (
    InsufficientQualityError,
    NoRelevantCasesError,
    NotEnoughData,
    RequestProcessor,
)


class FakeKadClient(KadClient):
    def count_cases(self, query_text: str, settings: Settings) -> int:
        return 5

    async def fetch_decisions(
        self,
        query_text: str,
        settings: Settings,
        on_progress=None,
        on_successful=None,
        on_retry=None,
        on_collection_progress=None,
        on_stage_change=None,
        should_cancel=None,
    ) -> FetchDecisionsResult:
        if on_stage_change:
            on_stage_change("collecting")
        if on_collection_progress:
            on_collection_progress(5)
        if on_stage_change:
            on_stage_change("analyzing")
        decisions = [
            CaseDecision(
                case_number="A40-1/2023",
                decision_date=date(2023, 3, 15),
                outcome=CaseOutcome.SATISFIED,
                reasons=("доказан вред",),
                court_name="АС города Москвы",
            ),
            CaseDecision(
                case_number="A40-2/2023",
                decision_date=date(2023, 4, 1),
                outcome=CaseOutcome.DENIED,
                reasons=("пропуск срока",),
                court_name="АС города Москвы",
            ),
            CaseDecision(
                case_number="A40-3/2023",
                decision_date=date(2023, 5, 1),
                outcome=CaseOutcome.DENIED,
                reasons=("пропуск срока",),
                court_name="АС города Москвы",
            ),
            CaseDecision(
                case_number="A40-4/2023",
                decision_date=date(2023, 6, 1),
                outcome=CaseOutcome.SATISFIED,
                reasons=("нарушение процедуры",),
                court_name="АС города Москвы",
            ),
            CaseDecision(
                case_number="A40-5/2023",
                decision_date=date(2023, 7, 1),
                outcome=CaseOutcome.DENIED,
                reasons=("пропуск срока",),
                court_name="АС города Москвы",
            ),
        ]
        if on_progress:
            on_progress(5)
        if on_successful:
            on_successful(5)
        return FetchDecisionsResult(
            decisions=decisions,
            stats=FetchStats(
                attempted_cases=5,
                successful_cases=5,
                retry_count=0,
                effective_concurrency=8,
                case_id_collection_ms=10,
                details_fetch_ms=20,
                filtered_by_court=0,
                court_compared_cases=5,
            ),
        )


class InMemoryLogRepository:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, str]] = []

    def append(self, hashed_user_id: str, query_text: str, result_summary: str) -> None:
        self.entries.append((hashed_user_id, query_text, result_summary))


class FailingAnalysisCacheRepository:
    def get(self, cache_key: str, now: datetime):
        return None

    def set(self, cache_key: str, result, now: datetime) -> None:
        raise sqlite3.OperationalError("unable to open database file")


class FailingLogRepository:
    def append(self, hashed_user_id: str, query_text: str, result_summary: str) -> None:
        raise sqlite3.OperationalError("unable to open database file")


def test_request_processor_caches_and_logs(tmp_path):
    settings = Settings(
        max_cases=500,
        max_documents_per_case=5,
        max_pages=20,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        min_known_outcomes=5,
        analysis_prompt="test",
        updated_at=datetime(2026, 2, 11, 12, 0, 0),
    )
    registry = ActiveRequestRegistry()
    user_id = UserId("123")
    registry.start(user_id, query_text="query 2023", total_cases=5)

    cache_repo = AnalysisCacheRepository(
        SqliteConnection(str(tmp_path / "app.db")), ttl_seconds=60
    )

    processor = RequestProcessor(
        kad_client=FakeKadClient(),
        analysis_service=AnalysisService(),
        active_requests=registry,
        cache_repository=cache_repo,
        log_repository=InMemoryLogRepository(),
        hashing_service=HashingService(salt="pepper"),
    )

    result = asyncio.run(processor.process(user_id, "query 2023", settings))

    assert "Всего верифицировано: 5" in result.summary
    assert "Суд: АС города Москвы" in result.summary
    assert registry.get(user_id).processed_cases == 5


def test_request_processor_returns_result_even_if_cache_and_log_write_fail():
    settings = Settings(
        max_cases=500,
        max_documents_per_case=5,
        max_pages=20,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        min_known_outcomes=5,
        analysis_prompt="test",
        updated_at=datetime(2026, 2, 11, 12, 0, 0),
    )
    registry = ActiveRequestRegistry()
    user_id = UserId("123")
    registry.start(user_id, query_text="query 2023", total_cases=5)

    processor = RequestProcessor(
        kad_client=FakeKadClient(),
        analysis_service=AnalysisService(),
        active_requests=registry,
        cache_repository=FailingAnalysisCacheRepository(),  # type: ignore[arg-type]
        log_repository=FailingLogRepository(),  # type: ignore[arg-type]
        hashing_service=HashingService(salt="pepper"),
    )

    result = asyncio.run(processor.process(user_id, "query 2023", settings))

    assert "Всего верифицировано: 5" in result.summary


class MostlyUnknownKadClient(KadClient):
    def count_cases(self, query_text: str, settings: Settings) -> int:
        return 60

    async def fetch_decisions(
        self,
        query_text: str,
        settings: Settings,
        on_progress=None,
        on_successful=None,
        on_retry=None,
        on_collection_progress=None,
        on_stage_change=None,
        should_cancel=None,
    ) -> FetchDecisionsResult:
        if on_stage_change:
            on_stage_change("analyzing")
        decisions = [
            CaseDecision(
                case_number=f"A40-{idx}/2024",
                decision_date=date(2024, 1, 1),
                outcome=CaseOutcome.UNKNOWN if idx < 30 else CaseOutcome.DENIED,
                reasons=(),
                court_name="АС города Москвы",
            )
            for idx in range(60)
        ]
        if on_progress:
            on_progress(60)
        if on_successful:
            on_successful(60)
        return FetchDecisionsResult(
            decisions=decisions,
            stats=FetchStats(
                attempted_cases=60,
                successful_cases=60,
                retry_count=0,
                effective_concurrency=8,
                case_id_collection_ms=10,
                details_fetch_ms=20,
                filtered_by_court=0,
                court_compared_cases=60,
            ),
        )


def test_request_processor_blocks_final_summary_on_quality_gate(tmp_path):
    settings = Settings(
        max_cases=500,
        max_documents_per_case=5,
        max_pages=20,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        analysis_prompt="test",
        updated_at=datetime(2026, 2, 11, 12, 0, 0),
        unknown_outcome_threshold_percent=15,
        min_known_outcomes=20,
    )
    registry = ActiveRequestRegistry()
    user_id = UserId("123")
    registry.start(user_id, query_text="query 2024", total_cases=60)
    cache_repo = AnalysisCacheRepository(
        SqliteConnection(str(tmp_path / "app.db")), ttl_seconds=60
    )

    processor = RequestProcessor(
        kad_client=MostlyUnknownKadClient(),
        analysis_service=AnalysisService(),
        active_requests=registry,
        cache_repository=cache_repo,
        log_repository=InMemoryLogRepository(),
        hashing_service=HashingService(salt="pepper"),
    )

    with pytest.raises(InsufficientQualityError) as exc:
        asyncio.run(processor.process(user_id, "query 2024", settings))

    assert exc.value.reason_code == "unknown_share_high"
    assert exc.value.total_cases == 60
    assert exc.value.unknown_cases == 30
    assert exc.value.case_list


class EmptyResultKadClient(KadClient):
    def count_cases(self, query_text: str, settings: Settings) -> int:
        return 500

    async def fetch_decisions(
        self,
        query_text: str,
        settings: Settings,
        on_progress=None,
        on_successful=None,
        on_retry=None,
        on_collection_progress=None,
        on_stage_change=None,
        should_cancel=None,
    ) -> FetchDecisionsResult:
        if on_stage_change:
            on_stage_change("analyzing")
        if on_progress:
            on_progress(3)
        return FetchDecisionsResult(
            decisions=[],
            stats=FetchStats(
                attempted_cases=3,
                successful_cases=0,
                retry_count=0,
                effective_concurrency=8,
                case_id_collection_ms=10,
                details_fetch_ms=20,
                filtered_by_court=0,
                court_compared_cases=0,
            ),
        )


def test_request_processor_raises_not_enough_data_when_no_decisions(tmp_path):
    settings = Settings(
        max_cases=500,
        max_documents_per_case=5,
        max_pages=20,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        analysis_prompt="test",
        updated_at=datetime(2026, 2, 11, 12, 0, 0),
    )
    registry = ActiveRequestRegistry()
    user_id = UserId("123")
    registry.start(user_id, query_text="query 2024", total_cases=500)
    cache_repo = AnalysisCacheRepository(
        SqliteConnection(str(tmp_path / "app.db")), ttl_seconds=60
    )

    processor = RequestProcessor(
        kad_client=EmptyResultKadClient(),
        analysis_service=AnalysisService(),
        active_requests=registry,
        cache_repository=cache_repo,
        log_repository=InMemoryLogRepository(),
        hashing_service=HashingService(salt="pepper"),
    )

    with pytest.raises(NotEnoughData):
        asyncio.run(processor.process(user_id, "query 2024", settings))


class ArticleFilteredOutKadClient(KadClient):
    def count_cases(self, query_text: str, settings: Settings) -> int:
        return 500

    async def fetch_decisions(
        self,
        query_text: str,
        settings: Settings,
        on_progress=None,
        on_successful=None,
        on_retry=None,
        on_collection_progress=None,
        on_stage_change=None,
        should_cancel=None,
    ) -> FetchDecisionsResult:
        if on_stage_change:
            on_stage_change("analyzing")
        if on_progress:
            on_progress(425)
        if on_successful:
            on_successful(425)
        return FetchDecisionsResult(
            decisions=[],
            stats=FetchStats(
                attempted_cases=425,
                successful_cases=425,
                retry_count=0,
                effective_concurrency=8,
                case_id_collection_ms=10,
                details_fetch_ms=20,
                filtered_by_court=0,
                court_compared_cases=425,
                filtered_by_article=425,
            ),
        )


def test_request_processor_raises_no_relevant_cases_after_article_filter(tmp_path):
    settings = Settings(
        max_cases=500,
        max_documents_per_case=5,
        max_pages=20,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        analysis_prompt="test",
        updated_at=datetime(2026, 2, 11, 12, 0, 0),
    )
    registry = ActiveRequestRegistry()
    user_id = UserId("123")
    registry.start(user_id, query_text="ст 61.2 2024", total_cases=500)
    cache_repo = AnalysisCacheRepository(
        SqliteConnection(str(tmp_path / "app.db")), ttl_seconds=60
    )

    processor = RequestProcessor(
        kad_client=ArticleFilteredOutKadClient(),
        analysis_service=AnalysisService(),
        active_requests=registry,
        cache_repository=cache_repo,
        log_repository=InMemoryLogRepository(),
        hashing_service=HashingService(salt="pepper"),
    )

    with pytest.raises(NoRelevantCasesError) as exc:
        asyncio.run(processor.process(user_id, "ст 61.2 2024", settings))

    assert exc.value.total_processed == 425
    assert exc.value.filtered_by_article == 425
