import asyncio
from datetime import date, datetime
import sqlite3

import httpx

from src.domain.entities import CaseDecision, CaseOutcome
from src.domain.settings import Settings
from src.infrastructure.case_details_cache_repository import CaseDetailsCacheRepository
from src.infrastructure.sqlite import SqliteConnection
from src.services.kad_client import DecisionFetchOutcome, KadUnavailableError, ParserApiKadClient


def build_settings(max_cases: int = 24) -> Settings:
    return Settings(
        max_cases=max_cases,
        max_documents_per_case=5,
        max_pages=20,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        analysis_prompt="test",
        updated_at=datetime(2026, 2, 11, 12, 0, 0),
    )


def test_adaptive_concurrency_increases_after_stable_windows():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")

    async def fake_sanitize(params):
        return params

    async def fake_collect(_params, _settings, should_cancel=None, on_collection_progress=None):
        return [f"id-{idx}" for idx in range(24)]

    async def fake_fetch(case_id, settings):
        idx = int(case_id.split("-")[1])
        return DecisionFetchOutcome(
            decision=CaseDecision(
                case_number=f"A40-{idx}/2023",
                decision_date=date(2023, 1, 1),
                outcome=CaseOutcome.DENIED,
                reasons=("пропуск срока",),
            ),
            retry_count=0,
            had_transient_error=False,
        )

    client._sanitize_params_async = fake_sanitize  # type: ignore[method-assign]
    client._collect_case_ids = fake_collect  # type: ignore[method-assign]
    client._fetch_case_decision_with_metrics = fake_fetch  # type: ignore[method-assign]

    result = asyncio.run(client.fetch_decisions("query", build_settings()))

    assert result.stats.effective_concurrency == 10
    assert result.stats.attempted_cases == 24
    assert len(result.decisions) == 24
    asyncio.run(client.aclose())


def test_adaptive_concurrency_decreases_on_transient_errors():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")

    async def fake_sanitize(params):
        return params

    async def fake_collect(_params, _settings, should_cancel=None, on_collection_progress=None):
        return [f"id-{idx}" for idx in range(14)]

    async def fake_fetch(case_id, settings):
        idx = int(case_id.split("-")[1])
        transient = idx < 8
        return DecisionFetchOutcome(
            decision=CaseDecision(
                case_number=f"A40-{idx}/2023",
                decision_date=date(2023, 1, 1),
                outcome=CaseOutcome.DENIED,
                reasons=("пропуск срока",),
            ),
            retry_count=1 if transient else 0,
            had_transient_error=transient,
        )

    client._sanitize_params_async = fake_sanitize  # type: ignore[method-assign]
    client._collect_case_ids = fake_collect  # type: ignore[method-assign]
    client._fetch_case_decision_with_metrics = fake_fetch  # type: ignore[method-assign]

    result = asyncio.run(client.fetch_decisions("query", build_settings(max_cases=14)))

    assert result.stats.effective_concurrency == 7
    assert result.stats.retry_count == 8
    asyncio.run(client.aclose())


def test_request_json_async_retries_429_then_succeeds(monkeypatch):
    class FakeAsyncClient:
        def __init__(self) -> None:
            self._responses = [
                httpx.Response(429, json={"error": "rate"}),
                httpx.Response(429, json={"error": "rate"}),
                httpx.Response(
                    200,
                    json={"Success": 1, "Cases": []},
                    request=httpx.Request("GET", "https://example.com/search"),
                ),
            ]

        async def request(self, method: str, url: str, params: dict, timeout: int):
            return self._responses.pop(0)

    client = ParserApiKadClient(base_url="https://example.com", api_key="token")
    client._async_http_client = FakeAsyncClient()  # type: ignore[assignment]
    client._owns_async_client = False  # type: ignore[attr-defined]

    async def fast_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    result = asyncio.run(client._request_json_async("GET", "/search", params={"key": "token"}))
    assert result.retry_count == 2
    assert result.had_transient_error is True
    assert result.data["Success"] == 1


def test_fetch_decisions_uses_case_details_cache(tmp_path):
    cache_repo = CaseDetailsCacheRepository(SqliteConnection(str(tmp_path / "app.db")), ttl_seconds=3600)
    cached_decision = CaseDecision(
        case_number="A40-1/2023",
        decision_date=date(2023, 3, 15),
        outcome=CaseOutcome.SATISFIED,
        reasons=("доказан вред",),
    )
    now = datetime.now()
    cache_repo.set("case-1", cached_decision, now)

    client = ParserApiKadClient(
        base_url="https://example.com",
        api_key="token",
        details_cache_repository=cache_repo,
    )

    async def fake_sanitize(params):
        return params

    async def fake_collect(_params, _settings, should_cancel=None, on_collection_progress=None):
        return ["case-1"]

    async def fail_request_json_async(method: str, path: str, params: dict):
        raise AssertionError("details_by_id request should not happen on cache hit")

    client._sanitize_params_async = fake_sanitize  # type: ignore[method-assign]
    client._collect_case_ids = fake_collect  # type: ignore[method-assign]
    client._request_json_async = fail_request_json_async  # type: ignore[method-assign]

    result = asyncio.run(client.fetch_decisions("query", build_settings(max_cases=1)))

    assert len(result.decisions) == 1
    assert result.decisions[0].case_number == "A40-1/2023"
    asyncio.run(client.aclose())


def test_fetch_decisions_continues_on_partial_unavailable_errors():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")

    async def fake_sanitize(params):
        return params

    async def fake_collect(_params, _settings, should_cancel=None, on_collection_progress=None):
        return [f"id-{idx}" for idx in range(12)]

    async def fake_fetch(case_id, settings):
        idx = int(case_id.split("-")[1])
        if idx % 4 == 0:
            raise KadUnavailableError("temporary")
        return DecisionFetchOutcome(
            decision=CaseDecision(
                case_number=f"A40-{idx}/2023",
                decision_date=date(2023, 1, 1),
                outcome=CaseOutcome.DENIED,
                reasons=("пропуск срока",),
            ),
            retry_count=0,
            had_transient_error=False,
        )

    client._sanitize_params_async = fake_sanitize  # type: ignore[method-assign]
    client._collect_case_ids = fake_collect  # type: ignore[method-assign]
    client._fetch_case_decision_with_metrics = fake_fetch  # type: ignore[method-assign]

    result = asyncio.run(client.fetch_decisions("query", build_settings(max_cases=12)))

    assert result.stats.attempted_cases == 12
    assert len(result.decisions) == 9
    asyncio.run(client.aclose())


def test_fetch_decisions_ignores_cache_write_operational_errors():
    class FailingCacheRepo:
        def get(self, case_id, now):
            return None

        def set(self, case_id, decision, now, ttl_seconds=None):
            raise sqlite3.OperationalError("unable to open database file")

    client = ParserApiKadClient(
        base_url="https://example.com",
        api_key="token",
        details_cache_repository=FailingCacheRepo(),  # type: ignore[arg-type]
    )

    async def fake_sanitize(params):
        return params

    async def fake_collect(_params, _settings, should_cancel=None, on_collection_progress=None):
        return ["case-1"]

    async def fake_request_json_async(method: str, path: str, params: dict):
        return type(
            "ReqResult",
            (),
            {
                "data": {
                    "Success": 1,
                    "Cases": [
                        {
                            "CaseNumber": "A40-1/2023",
                            "Court": {"Code": "MSK", "Name": "АС города Москвы"},
                            "CaseInstances": [
                                {
                                    "InstanceEvents": [
                                        {
                                            "Date": "2023-03-15",
                                            "DecisionTypeName": "Отказано",
                                        }
                                    ]
                                }
                            ],
                        }
                    ],
                },
                "retry_count": 0,
                "had_transient_error": False,
            },
        )()

    client._sanitize_params_async = fake_sanitize  # type: ignore[method-assign]
    client._collect_case_ids = fake_collect  # type: ignore[method-assign]
    client._request_json_async = fake_request_json_async  # type: ignore[method-assign]

    result = asyncio.run(client.fetch_decisions("query", build_settings(max_cases=1)))

    assert result.stats.attempted_cases == 1
    assert len(result.decisions) == 1
    assert result.decisions[0].case_link == "https://kad.arbitr.ru/Card/case-1"
    assert result.decisions[0].court_name == "АС города Москвы"
    asyncio.run(client.aclose())


def test_fetch_decisions_filters_out_other_courts_when_court_is_requested():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")

    async def fake_sanitize(params):
        return params

    async def fake_collect(_params, _settings, should_cancel=None, on_collection_progress=None):
        return ["id-1", "id-2", "id-3"]

    async def fake_fetch(case_id, settings):
        court_map = {
            "id-1": "АС города Москвы",
            "id-2": "АС Санкт-Петербурга и Ленинградской области",
            "id-3": "",
        }
        return DecisionFetchOutcome(
            decision=CaseDecision(
                case_number=f"A40-{case_id}/2025",
                decision_date=date(2025, 1, 1),
                outcome=CaseOutcome.DENIED,
                reasons=(),
                court_name=court_map[case_id],
            ),
            retry_count=0,
            had_transient_error=False,
        )

    client._sanitize_params_async = fake_sanitize  # type: ignore[method-assign]
    client._collect_case_ids = fake_collect  # type: ignore[method-assign]
    client._fetch_case_decision_with_metrics = fake_fetch  # type: ignore[method-assign]

    result = asyncio.run(client.fetch_decisions("Практика в АС СПб за 2025 год", build_settings(max_cases=3)))

    assert result.stats.attempted_cases == 3
    assert len(result.decisions) == 2
    assert all("Санкт-Петербурга" in decision.court_name for decision in result.decisions)
    asyncio.run(client.aclose())


def test_fetch_decisions_does_not_drop_cases_on_article_only_query():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")

    async def fake_sanitize(params):
        return params

    async def fake_collect(_params, _settings, should_cancel=None, on_collection_progress=None):
        return ["id-1", "id-2"]

    async def fake_fetch(case_id, settings):
        text_map = {
            "id-1": "Применена ст. 61.3 Закона о банкротстве, заявление удовлетворено.",
            "id-2": "Рассмотрено требование конкурсного управляющего по ст. 61.3, в иске отказано.",
        }
        return DecisionFetchOutcome(
            decision=CaseDecision(
                case_number=f"A40-{case_id}/2025",
                decision_date=date(2025, 1, 1),
                outcome=CaseOutcome.DENIED,
                reasons=("оценка обстоятельств дела",),
                court_name="АС города Москвы",
                analysis_text=text_map[case_id],
            ),
            retry_count=0,
            had_transient_error=False,
        )

    client._sanitize_params_async = fake_sanitize  # type: ignore[method-assign]
    client._collect_case_ids = fake_collect  # type: ignore[method-assign]
    client._fetch_case_decision_with_metrics = fake_fetch  # type: ignore[method-assign]

    result = asyncio.run(client.fetch_decisions("Практика по ст.61.3 в АС Москвы за 2025 год", build_settings(max_cases=2)))

    assert result.stats.attempted_cases == 2
    assert len(result.decisions) == 2
    asyncio.run(client.aclose())


def test_fetch_decisions_filters_by_requested_paragraph():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")

    async def fake_sanitize(params):
        return params

    async def fake_collect(_params, _settings, should_cancel=None, on_collection_progress=None):
        return ["id-1", "id-2"]

    async def fake_fetch(case_id, settings):
        text_map = {
            "id-1": "Суд применил п. 2 ст. 61.3 Закона о банкротстве.",
            "id-2": "Суд применил п. 1 ст. 61.3 Закона о банкротстве.",
        }
        return DecisionFetchOutcome(
            decision=CaseDecision(
                case_number=f"A40-{case_id}/2025",
                decision_date=date(2025, 1, 1),
                outcome=CaseOutcome.SATISFIED,
                reasons=("оспаривание сделки по ст. 61.3",),
                court_name="АС города Москвы",
                analysis_text=text_map[case_id],
            ),
            retry_count=0,
            had_transient_error=False,
        )

    client._sanitize_params_async = fake_sanitize  # type: ignore[method-assign]
    client._collect_case_ids = fake_collect  # type: ignore[method-assign]
    client._fetch_case_decision_with_metrics = fake_fetch  # type: ignore[method-assign]

    result = asyncio.run(client.fetch_decisions("Практика по п.2 ст.61.3 в АС Москвы за 2025 год", build_settings(max_cases=2)))

    assert result.stats.attempted_cases == 2
    assert len(result.decisions) == 1
    assert "п. 2" in result.decisions[0].analysis_text
    asyncio.run(client.aclose())


def test_extract_outcome_and_reasons_uses_final_decisive_event():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")
    events = [
        {
            "Date": "2024-01-10",
            "DecisionTypeName": "Удовлетворено",
            "AdditionalInfo": "нарушение процедуры",
        },
        {
            "Date": "2024-01-20",
            "DecisionTypeName": "Отказано",
            "AdditionalInfo": "пропуск срока",
        },
    ]

    outcome, reasons, decision_date, analysis_text, document_links, reason_conf = client._extract_outcome_and_reasons(events)  # noqa: SLF001

    assert outcome == CaseOutcome.DENIED
    assert any("пропуск срока" in r for r in reasons)
    assert decision_date == date(2024, 1, 20)
    assert analysis_text
    asyncio.run(client.aclose())


def test_extract_outcome_and_reasons_uses_latest_decisive_event_when_last_event_is_unclear():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")
    events = [
        {
            "Date": "2024-01-10",
            "DecisionTypeName": "Удовлетворено",
            "AdditionalInfo": "нарушение процедуры",
        },
        {
            "Date": "2024-01-20",
            "DecisionTypeName": "Назначено заседание",
            "AdditionalInfo": "",
        },
    ]

    outcome, reasons, decision_date, analysis_text, document_links, reason_conf = client._extract_outcome_and_reasons(events)  # noqa: SLF001

    assert outcome == CaseOutcome.SATISFIED
    assert decision_date == date(2024, 1, 10)
    assert reasons
    assert analysis_text
    asyncio.run(client.aclose())


def test_extract_outcome_and_reasons_returns_unknown_when_no_decisive_events():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")
    events = [
        {
            "Date": "2024-01-20",
            "DecisionTypeName": "Назначено заседание",
            "AdditionalInfo": "",
        },
    ]

    outcome, reasons, decision_date, analysis_text, document_links, reason_conf = client._extract_outcome_and_reasons(events)  # noqa: SLF001

    assert outcome == CaseOutcome.UNKNOWN
    assert reasons == ("оценка обстоятельств дела",)
    assert decision_date == date(2024, 1, 20)
    assert analysis_text
    asyncio.run(client.aclose())
