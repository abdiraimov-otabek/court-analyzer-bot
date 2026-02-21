import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.entities import CaseDecision, CaseOutcome
from src.services.llm_reason_extractor import LLMReasonExtractor


def _mock_response(body_json):
    mock = MagicMock()
    mock.status_code = 200
    mock.json.return_value = {
        "choices": [{"message": {"content": json.dumps(body_json)}}]
    }
    mock.raise_for_status = lambda: None
    return mock


@pytest.mark.asyncio
async def test_golden_dataset_verifiability():
    """
    Formal verification of 'Perfection' requirements:
    1. Substantive bankruptcy acts are ACCEPTED.
    2. Procedural bankruptcy acts (scheduling etc) are REJECTED.
    3. Non-bankruptcy acts mentioning the article are REJECTED.
    """
    http = AsyncMock()
    extractor = LLMReasonExtractor(api_key="test", http_client=http)

    # 1. GOLDEN RELEVANT: Substantive application of 61.2
    substantive_act = "Суд установил, что сделка совершена при неравноценном встречном исполнении (ст. 61.2 Закона о банкротстве)..."
    api_response_relevant = [
        {
            "relevant": True,
            "reasons": ["неравноценное исполнение"],
            "proof_quote": "сделка совершена при неравноценном встречном исполнении",
            "outcome": "satisfied",
        }
    ]
    http.post.return_value = _mock_response(api_response_relevant)

    d1 = CaseDecision(
        case_number="A40-1/2024",
        decision_date="2024-01-01",
        outcome=CaseOutcome.SATISFIED,
        reasons=(),
        analysis_text=substantive_act,
        case_category="Б",
    )
    is_rel, reasons, quote, llm_outcome = await extractor.classify_and_extract(
        d1, "61.2", "ст. 61.2 банкротство"
    )
    assert is_rel is True
    assert "неравноценное" in reasons[0]
    assert len(quote) > 5
    assert llm_outcome == "satisfied"

    # 2. GOLDEN IRRELEVANT: Procedural act (scheduling)
    procedural_act = "Назначить судебное разбирательство по заявлению об оспаривании сделки (ст. 61.2 Закона о банкротстве) на 10.10.2024."
    api_response_irrelevant = [
        {"relevant": False, "reasons": ["ПРОЦЕДУРНЫЙ_АКТ"], "proof_quote": "", "outcome": "unknown"}
    ]
    http.post.return_value = _mock_response(api_response_irrelevant)

    d2 = CaseDecision(
        case_number="A40-2/2024",
        decision_date="2024-01-01",
        outcome=CaseOutcome.UNKNOWN,
        reasons=(),
        analysis_text=procedural_act,
        case_category="Б",
    )
    is_rel, _, _, _ = await extractor.classify_and_extract(
        d2, "61.2", "ст. 61.2 банкротство"
    )
    assert is_rel is False

    # 3. GOLDEN IRRELEVANT: Context mismatch (Civil case mentioning the article in passing)
    civil_act = "Истец ссылается на аналогию со ст. 61.2 Закона о банкротстве, однако данный спор вытекает из договора подряда."
    api_response_mismatch = [
        {"relevant": False, "reasons": ["КОНТЕКСТНЫЙ_ДИССОНАНС"], "proof_quote": "", "outcome": "unknown"}
    ]
    http.post.return_value = _mock_response(api_response_mismatch)

    d3 = CaseDecision(
        case_number="A40-3/2024",
        decision_date="2024-01-01",
        outcome=CaseOutcome.SATISFIED,
        reasons=(),
        analysis_text=civil_act,
        case_category="Г",
    )
    is_rel, _, _, _ = await extractor.classify_and_extract(
        d3, "61.2", "ст. 61.2 банкротство"
    )
    assert is_rel is False
