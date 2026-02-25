"""Regression test: proves that relevant cases without proof_quote are NOT rejected.

This test reproduces the exact bug scenario where ~500 cases from KAD API
were filtered down to just 1 because the LLM marked them as relevant but
couldn't provide a verbatim proof_quote. The old "Zero-Hallucination" rule
rejected every such case. After the fix, they should survive.
"""
from __future__ import annotations

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
        decision_date="2024-06-01",
        outcome=CaseOutcome.DENIED,
        reasons=("оценка обстоятельств дела",),
        case_id=f"case-{i}",
        court_name="АС города Москвы",
        analysis_text=f"Определение по делу А40-{i}/2024 об оспаривании сделки по ст. 61.2",
        case_category="Б",
    )


@pytest.mark.asyncio
async def test_500_cases_with_no_proof_quote_are_not_filtered_to_1():
    """
    Exact reproduction of the bug:
    - 10 cases sent to classify_batch (simulating the 500-case scenario in batches)
    - LLM says all 10 are relevant (relevant: true)
    - But only 1 has a proof_quote; the other 9 have proof_quote: ""
    - OLD behavior: only 1 survives (the one with the quote)
    - NEW behavior: all 10 survive (the 9 without quotes get НЕТ_ЦИТАТЫ prefix)
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

    # THE CRITICAL ASSERTION: all 10 should be relevant, not just 1
    assert relevant_count == 10, (
        f"Expected all 10 cases to be relevant, but only {relevant_count} survived. "
        f"This means the proof_quote filter is still rejecting cases!"
    )

    # Verify that cases without quotes get the НЕТ_ЦИТАТЫ prefix
    for i, (is_rel, reasons, quote, outcome) in enumerate(results):
        assert is_rel is True, f"Case {i} should be relevant"
        if i == 0:
            assert not quote.startswith("НЕТ_ЦИТАТЫ"), "Case 0 has a real quote"
        else:
            assert quote.startswith("НЕТ_ЦИТАТЫ"), f"Case {i} should have НЕТ_ЦИТАТЫ prefix"
        assert outcome == "denied"


@pytest.mark.asyncio
async def test_irrelevant_cases_are_still_rejected():
    """Ensure we didn't break the legitimate rejection of irrelevant cases."""
    http = AsyncMock()
    extractor = LLMReasonExtractor(api_key="test", http_client=http)

    decisions = [_make_decision(i) for i in range(5)]

    # LLM says 2 are relevant, 3 are not
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
    assert relevant_count == 2, "Only 2 cases should be relevant"

    # Case 0: relevant with real quote
    assert results[0][0] is True
    assert not results[0][2].startswith("НЕТ_ЦИТАТЫ")

    # Case 1: relevant without quote → НЕТ_ЦИТАТЫ
    assert results[1][0] is True
    assert results[1][2].startswith("НЕТ_ЦИТАТЫ")

    # Cases 2-4: irrelevant (LLM said so)
    for i in range(2, 5):
        assert results[i][0] is False


@pytest.mark.asyncio
async def test_single_case_classify_api_no_quote_stays_relevant():
    """Test the single-case _call_classify_api path too."""
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

    assert is_relevant is True, "Should stay relevant even without quote"
    assert quote.startswith("НЕТ_ЦИТАТЫ"), "Should have НЕТ_ЦИТАТЫ prefix"
    assert "неравноценное" in reasons[0]
