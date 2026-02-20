import asyncio
from datetime import datetime

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
