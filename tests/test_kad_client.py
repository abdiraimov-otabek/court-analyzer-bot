import asyncio
from datetime import date, datetime

from src.domain.entities import CaseDecision, CaseOutcome
from src.domain.settings import Settings
from src.services.kad_client import KadInvalidResponseError, ParserApiKadClient


def build_settings(max_cases: int = 500, max_pages: int = 20) -> Settings:
    return Settings(
        max_cases=max_cases,
        max_documents_per_case=5,
        max_pages=max_pages,
        fetch_concurrency_min=6,
        fetch_concurrency_max=10,
        slow_alert_minutes=5,
        details_cache_ttl_seconds=24 * 60 * 60,
        analysis_prompt="test",
        updated_at=datetime(2026, 2, 11, 12, 0, 0),
    )


def test_count_cases_fast_overflow_check_uses_first_page_only():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")
    calls: list[int] = []

    client._sanitize_params = lambda params: params  # type: ignore[method-assign]

    def fake_search(_params, page: int):
        calls.append(page)
        if page != 1:
            raise AssertionError("count_cases should stop after first page for >500 estimate")
        return {
            "Success": 1,
            "PagesCount": 40,
            "Cases": [{}] * 25,
        }

    client._search = fake_search  # type: ignore[method-assign]

    result = client.count_cases("анализ 9 ААС 2023", build_settings())

    assert result == 501
    assert calls == [1]
    asyncio.run(client.aclose())


def test_count_cases_fetches_all_pages_when_estimate_is_within_limit():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")
    calls: list[int] = []

    client._sanitize_params = lambda params: params  # type: ignore[method-assign]

    def fake_search(_params, page: int):
        calls.append(page)
        if page == 1:
            return {
                "Success": 1,
                "PagesCount": 2,
                "Cases": [{}] * 20,
            }
        if page == 2:
            return {
                "Success": 1,
                "PagesCount": 2,
                "Cases": [{}] * 7,
            }
        raise AssertionError(f"Unexpected page {page}")

    client._search = fake_search  # type: ignore[method-assign]

    result = client.count_cases("анализ АС Москвы 2023", build_settings())

    assert result == 27
    assert calls == [1, 2]
    asyncio.run(client.aclose())


def test_sanitize_params_falls_back_when_court_filter_is_invalid():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")
    calls: list[dict] = []

    def fake_request_json(method: str, path: str, params: dict):
        calls.append(params.copy())
        if "Court" in params:
            raise KadInvalidResponseError("Invalid request: Court")
        return {"Success": 1, "PagesCount": 0, "Cases": []}

    client._request_json = fake_request_json  # type: ignore[method-assign]
    params = client._parser.parse("Практика по ст.61.2 в АС Новосибирска за 2025 год")

    sanitized = client._sanitize_params(params)  # noqa: SLF001

    assert sanitized.court == "АС Новосибирска"
    assert sanitized.use_court_filter is False
    assert any("Court" in call for call in calls)
    assert any("Court" not in call for call in calls)
    asyncio.run(client.aclose())


def test_refine_params_with_llm_normalizes_cyrillic_case_type_to_latin():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")

    class StubLLM:
        async def parse_query(self, _query_text: str):
            return {
                "article": "723",
                "full_article": "ст. 723 ГК РФ",
                "court": "АС Москвы",
                "year": 2025,
                "quarter": 1,
                "case_type": "Г",
                "date_from": "2025-01-01",
                "date_to": "2025-03-31",
            }

    client._llm_reason_extractor = StubLLM()  # type: ignore[assignment]
    params = client._parser.parse("Практика по статье 723 в АС Москвы за 2025 год")

    refined = asyncio.run(client._refine_params_with_llm("query", params))  # noqa: SLF001

    assert refined.case_type == "G"
    asyncio.run(client.aclose())


def test_enrich_unknown_outcomes_with_llm_updates_unknown_statuses():
    client = ParserApiKadClient(base_url="https://example.com", api_key="token")

    class StubLLM:
        async def extract_with_outcome(self, decision: CaseDecision):
            if decision.case_number.endswith("1"):
                return ("признание сделки недействительной",), "satisfied"
            return ("оценка обстоятельств дела",), None

    client._llm_reason_extractor = StubLLM()  # type: ignore[assignment]
    decisions = [
        CaseDecision(
            case_number="А40-1/2025",
            decision_date=date(2025, 1, 1),
            outcome=CaseOutcome.UNKNOWN,
            reasons=("оценка обстоятельств дела",),
        ),
        CaseDecision(
            case_number="А40-2/2025",
            decision_date=date(2025, 1, 1),
            outcome=CaseOutcome.UNKNOWN,
            reasons=("оценка обстоятельств дела",),
        ),
    ]

    enriched = asyncio.run(client._enrich_unknown_outcomes_with_llm(decisions))  # noqa: SLF001

    assert enriched[0].outcome == CaseOutcome.SATISFIED
    assert enriched[0].reasons == ("признание сделки недействительной",)
    assert enriched[1].outcome == CaseOutcome.UNKNOWN
    asyncio.run(client.aclose())
