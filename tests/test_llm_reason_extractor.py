from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.entities import CaseDecision, CaseOutcome
from src.services.llm_reason_extractor import LLMReasonExtractor


def _make_extractor(http_client=None, api_key="test-key", **kwargs):
    if http_client is None:
        http_client = AsyncMock()
    return LLMReasonExtractor(http_client=http_client, api_key=api_key, **kwargs)


def _mock_response(body: object, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = body
    if status_code >= 400:
        from httpx import HTTPStatusError, Response, Request
        import httpx
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=MagicMock(status_code=status_code)
        )
    else:
        resp.raise_for_status = MagicMock()
    return resp


def _api_body(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


@pytest.mark.asyncio
async def test_valid_response_returns_canonical_labels():
    http = AsyncMock()
    labels = (
        "неравноценное встречное исполнение (п.1 ст.61.2)",
        "аффилированность сторон",
    )
    http.post = AsyncMock(return_value=_mock_response(_api_body(json.dumps(labels, ensure_ascii=False))))
    extractor = _make_extractor(http_client=http)

    result = await extractor.extract("Отказать в удовлетворении / Решение", CaseOutcome.DENIED)

    assert result == labels
    http.post.assert_called_once()


@pytest.mark.asyncio
async def test_non_canonical_label_filtered_returns_fallback():
    http = AsyncMock()
    body = json.dumps(["выдуманное основание", "другое несуществующее"], ensure_ascii=False)
    http.post = AsyncMock(return_value=_mock_response(_api_body(body)))
    extractor = _make_extractor(http_client=http)

    result = await extractor.extract("some text", CaseOutcome.SATISFIED)

    assert result == ("оценка обстоятельств дела",)


@pytest.mark.asyncio
async def test_http_error_returns_fallback():
    import httpx
    http = AsyncMock()
    http.post = AsyncMock(side_effect=httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock(status_code=500)
    ))
    extractor = _make_extractor(http_client=http)

    result = await extractor.extract("some text", CaseOutcome.DENIED)

    assert result == ("оценка обстоятельств дела",)


@pytest.mark.asyncio
async def test_malformed_json_returns_fallback():
    http = AsyncMock()
    http.post = AsyncMock(return_value=_mock_response(_api_body("not valid json at all")))
    extractor = _make_extractor(http_client=http)

    result = await extractor.extract("some text", CaseOutcome.DENIED)

    assert result == ("оценка обстоятельств дела",)


@pytest.mark.asyncio
async def test_empty_text_returns_fallback_without_api_call():
    http = AsyncMock()
    extractor = _make_extractor(http_client=http)

    result = await extractor.extract("", CaseOutcome.UNKNOWN)

    assert result == ("оценка обстоятельств дела",)
    http.post.assert_not_called()


@pytest.mark.asyncio
async def test_whitespace_only_text_returns_fallback_without_api_call():
    http = AsyncMock()
    extractor = _make_extractor(http_client=http)

    result = await extractor.extract("   \n  ", CaseOutcome.UNKNOWN)

    assert result == ("оценка обстоятельств дела",)
    http.post.assert_not_called()


@pytest.mark.asyncio
async def test_mixed_valid_and_invalid_labels_returns_only_valid():
    http = AsyncMock()
    labels = (
        "неравноценное встречное исполнение (п.1 ст.61.2)",
        "несуществующее основание",
        "пропуск срока исковой давности",
    )
    http.post = AsyncMock(return_value=_mock_response(_api_body(json.dumps(labels, ensure_ascii=False))))
    extractor = _make_extractor(http_client=http)

    result = await extractor.extract("text", CaseOutcome.DENIED)

    assert result == (
        "неравноценное встречное исполнение (п.1 ст.61.2)",
        "пропуск срока исковой давности",
    )


@pytest.mark.asyncio
async def test_llm_returns_fallback_label_passes_through():
    """LLM explicitly returning "оценка обстоятельств дела" is allowed."""
    http = AsyncMock()
    body = json.dumps(["оценка обстоятельств дела"], ensure_ascii=False)
    http.post = AsyncMock(return_value=_mock_response(_api_body(body)))
    extractor = _make_extractor(http_client=http)

    result = await extractor.extract("no useful text", CaseOutcome.UNKNOWN)

    assert result == ("оценка обстоятельств дела",)


@pytest.mark.asyncio
async def test_non_list_json_response_returns_fallback():
    http = AsyncMock()
    http.post = AsyncMock(return_value=_mock_response(_api_body('{"key": "value"}')))
    extractor = _make_extractor(http_client=http)

    result = await extractor.extract("some text", CaseOutcome.DENIED)

    assert result == ("оценка обстоятельств дела",)


@pytest.mark.asyncio
async def test_markdown_fenced_json_is_parsed_correctly():
    """GPT-4o-mini often wraps JSON in ```json ... ``` fences."""
    http = AsyncMock()
    canonical = "пропуск срока исковой давности"
    fenced = f"```json\n[\"{canonical}\"]\n```"
    http.post = AsyncMock(return_value=_mock_response(_api_body(fenced)))
    extractor = _make_extractor(http_client=http)

    result = await extractor.extract("some text", CaseOutcome.DENIED)

    assert result == (canonical,)


@pytest.mark.asyncio
async def test_semaphore_limits_concurrency():
    """Verify semaphore is acquired for each call."""
    http = AsyncMock()
    canonical = "неравноценное встречное исполнение (п.1 ст.61.2)"
    http.post = AsyncMock(
        return_value=_mock_response(_api_body(json.dumps([canonical], ensure_ascii=False)))
    )
    extractor = _make_extractor(http_client=http, max_concurrent=2)

    tasks = [
        extractor.extract(f"text {i}", CaseOutcome.DENIED)
        for i in range(5)
    ]
    results = await asyncio.gather(*tasks)

    assert all(r == (canonical,) for r in results)
    assert http.post.call_count == 5


# --- classify_and_extract tests ---

from datetime import date as _date


def _make_decision(
    analysis_text: str = "Отказать в удовлетворении / Определение",
    outcome: CaseOutcome = CaseOutcome.DENIED,
    case_number: str = "А40-12345/2024",
    court_name: str = "АС города Москвы",
    case_category: str = "Б",
) -> CaseDecision:
    return CaseDecision(
        case_number=case_number,
        decision_date=_date(2024, 6, 1),
        outcome=outcome,
        reasons=("оценка обстоятельств дела",),
        case_id="test-id",
        court_name=court_name,
        analysis_text=analysis_text,
        case_category=case_category,
    )


@pytest.mark.asyncio
async def test_classify_relevant_case_returns_true_and_reasons():
    http = AsyncMock()
    labels = ["неравноценное встречное исполнение (п.1 ст.61.2)", "аффилированность сторон"]
    http.post = AsyncMock(return_value=_mock_response(_api_body(json.dumps(labels, ensure_ascii=False))))
    extractor = _make_extractor(http_client=http)

    decision = _make_decision("Признать сделку недействительной / Определение")
    is_relevant, reasons = await extractor.classify_and_extract(decision, "61.2")

    assert is_relevant is True
    assert reasons == tuple(labels)
    http.post.assert_called_once()


@pytest.mark.asyncio
async def test_classify_irrelevant_case_returns_false():
    http = AsyncMock()
    http.post = AsyncMock(
        return_value=_mock_response(_api_body(json.dumps(["НЕ_РЕЛЕВАНТНО"], ensure_ascii=False)))
    )
    extractor = _make_extractor(http_client=http)

    decision = _make_decision("Включить в реестр требований кредиторов", CaseOutcome.SATISFIED)
    is_relevant, reasons = await extractor.classify_and_extract(decision, "61.2")

    assert is_relevant is False
    assert reasons == ("НЕ_РЕЛЕВАНТНО",)


@pytest.mark.asyncio
async def test_classify_empty_text_returns_not_relevant():
    http = AsyncMock()
    extractor = _make_extractor(http_client=http)

    decision = _make_decision(
        analysis_text="", case_number="", court_name="", case_category=""
    )
    is_relevant, reasons = await extractor.classify_and_extract(decision, "61.2")

    assert is_relevant is False
    assert reasons == ("НЕ_РЕЛЕВАНТНО",)
    http.post.assert_not_called()


@pytest.mark.asyncio
async def test_classify_http_error_returns_true_fallback():
    """On error, keep the case (safe default) with generic reason."""
    import httpx as _httpx
    http = AsyncMock()
    http.post = AsyncMock(side_effect=_httpx.HTTPStatusError(
        "500", request=MagicMock(), response=MagicMock(status_code=500)
    ))
    extractor = _make_extractor(http_client=http)

    decision = _make_decision("some text")
    is_relevant, reasons = await extractor.classify_and_extract(decision, "61.2")

    assert is_relevant is True
    assert reasons == ("оценка обстоятельств дела",)


@pytest.mark.asyncio
async def test_classify_malformed_json_returns_true_fallback():
    http = AsyncMock()
    http.post = AsyncMock(return_value=_mock_response(_api_body("not json")))
    extractor = _make_extractor(http_client=http)

    decision = _make_decision("some text")
    is_relevant, reasons = await extractor.classify_and_extract(decision, "61.3")

    assert is_relevant is True
    assert reasons == ("оценка обстоятельств дела",)


@pytest.mark.asyncio
async def test_classify_uses_separate_cache():
    http = AsyncMock()
    labels = ["подозрительность сделки (ст.61.2)"]
    http.post = AsyncMock(return_value=_mock_response(_api_body(json.dumps(labels, ensure_ascii=False))))
    extractor = _make_extractor(http_client=http)

    decision = _make_decision("same text")
    r1 = await extractor.classify_and_extract(decision, "61.2")
    r2 = await extractor.classify_and_extract(decision, "61.2")

    assert r1 == r2
    assert http.post.call_count == 1  # second call served from cache


@pytest.mark.asyncio
async def test_classify_budget_exhausted_keeps_case():
    http = AsyncMock()
    extractor = _make_extractor(http_client=http)
    extractor.set_fetch_budget(0)

    decision = _make_decision("some text")
    is_relevant, reasons = await extractor.classify_and_extract(decision, "61.2")

    assert is_relevant is True  # safe default: keep when budget exhausted
    assert reasons == ("оценка обстоятельств дела",)
    http.post.assert_not_called()


@pytest.mark.asyncio
async def test_classify_prompt_includes_case_context():
    """Verify the prompt sent to LLM contains case number, court, category."""
    http = AsyncMock()
    labels = ["подозрительность сделки (ст.61.2)"]
    http.post = AsyncMock(return_value=_mock_response(_api_body(json.dumps(labels, ensure_ascii=False))))
    extractor = _make_extractor(http_client=http)

    decision = _make_decision(
        "Оспаривание сделки / Определение",
        case_number="А40-99999/2024",
        court_name="АС города Москвы",
        case_category="Б",
    )
    await extractor.classify_and_extract(decision, "61.2")

    call_args = http.post.call_args
    prompt = call_args.kwargs["json"]["messages"][0]["content"]
    assert "А40-99999/2024" in prompt
    assert "Москвы" in prompt
    assert "Банкротство" in prompt
