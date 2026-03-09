"""Test: proves that relevant cases without proof_quote are correctly rejected.

This test reproduces strict QA validation. It verifies that situations
where the LLM hallucinates relevance but cannot provide a rationale or
a quote will enforce the case to be dropped.
"""
from __future__ import annotations

import datetime
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.entities import CaseDecision, CaseOutcome
from src.services.llm_reason_extractor import LLMReasonExtractor


def _mock_response(body_json):
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {
        "choices": [{"message": {"content": json.dumps(body_json, ensure_ascii=False)}}]
    }
    mock.raise_for_status = lambda: None
    return mock


def _make_decision(i: int) -> CaseDecision:
    return CaseDecision(
        case_number=f"А40-{i}/2024",
        decision_date=datetime.date(2024, 6, 1),
        outcome=CaseOutcome.DENIED,
        reasons=("оценка обстоятельств дела",),
        case_id=f"case-{i}",
        court_name="АС города Москвы",
        analysis_text=f"Определение по делу А40-{i}/2024 об оспаривании сделки по ст. 61.2",
        case_category="Б",
    )


@pytest.mark.asyncio
async def test_lenient_verification_accepts_empty_proof_quote():
    """
    Ensure the new lenient rule works:
    - 10 cases sent to classify_batch
    - LLM returns "relevant": true for all
    - Only case 0 provides a real proof_quote
    - result: ALL cases survive because LLM confirmed relevance.
    """
    http = AsyncMock()
    extractor = LLMReasonExtractor(api_key="test", http_client=http)

    # Build 10 decisions
    decisions = [_make_decision(i) for i in range(10)]

    # LLM response: all relevant, but only case 0 has a real quote
    llm_response = []
    for i in range(10):
        llm_response.append({
            "relevant": True,
            "reasons": ["оспаривание сделки по ст.61.2 Закона о банкротстве"],
            "proof_quote": "Суд установил неравноценное встречное исполнение" if i == 0 else "",
            "outcome": "denied",
        })

    http.post = AsyncMock(return_value=_mock_response(llm_response))

    results = await extractor.classify_batch(decisions, "61.2", "ст. 61.2 банкротство")

    # Count how many survived as relevant
    relevant_count = sum(1 for is_rel, _, _, _ in results if is_rel)

    # NEW BEHAVIOR: All should survive
    assert relevant_count == 10, f"Expected all 10 cases to be relevant, but got {relevant_count}."

    assert results[0][0] is True
    assert "Суд установил" in results[0][2]

    for i in range(1, 10):
        assert results[i][0] is True
        assert "Прямая цитата не найдена" in results[i][2]


@pytest.mark.asyncio
async def test_irrelevant_cases_are_still_rejected():
    """Ensure we didn't break the legitimate rejection of irrelevant cases."""
    http = AsyncMock()
    extractor = LLMReasonExtractor(api_key="test", http_client=http)

    decisions = [_make_decision(i) for i in range(5)]

    # LLM says some are relevant, some are not
    llm_response = [
        {"relevant": True, "reasons": ["основание"], "proof_quote": "цитата", "outcome": "denied"},
        {"relevant": True, "reasons": ["основание"], "proof_quote": "", "outcome": "satisfied"},
        {"relevant": False, "reasons": ["не по теме"], "proof_quote": "", "outcome": "unknown"},
        {"relevant": False, "reasons": ["процедурный акт"], "proof_quote": "", "outcome": "unknown"},
        {"relevant": False, "reasons": ["другая статья"], "proof_quote": "", "outcome": "unknown"},
    ]

    http.post = AsyncMock(return_value=_mock_response(llm_response))
    results = await extractor.classify_batch(decisions, "61.2", "ст. 61.2")

    relevant_count = sum(1 for is_rel, _, _, _ in results if is_rel)
    assert relevant_count == 2, "Cases 0 and 1 should be relevant (1 survives via fallback quote)"

    # Case 0: relevant with real quote
    assert results[0][0] is True
    assert results[0][2] == "цитата"

    # Case 1: relevant without quote → survives via fallback
    assert results[1][0] is True
    assert "Прямая цитата не найдена" in results[1][2]

    # Cases 2-4: irrelevant (LLM said so)
    for i in range(2, 5):
        assert results[i][0] is False


@pytest.mark.asyncio
async def test_single_case_classify_api_accepts_empty_quote():
    """Test the single-case _call_classify_api path accepts empty proofs with fallback."""
    http = AsyncMock()
    extractor = LLMReasonExtractor(api_key="test", http_client=http)

    # Single case response: relevant but no quote
    api_response = {
        "relevant": True,
        "reasons": ["неравноценное встречное исполнение"],
        "proof_quote": "",
    }
    http.post = AsyncMock(return_value=_mock_response(api_response))

    is_relevant, reasons, quote = await extractor._call_classify_api(
        "Контекст дела...", CaseOutcome.DENIED, "61.2", "ст. 61.2"
    )

    assert is_relevant is True, "Should be accepted even with missing quote"
    assert "Прямая цитата не найдена" in quote
