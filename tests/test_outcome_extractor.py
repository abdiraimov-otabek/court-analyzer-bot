import asyncio
from datetime import date

from src.domain.entities import CaseDecision, CaseOutcome
from src.services.pipeline.validators.outcome import IssueOutcomeExtractor


class _StubLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def extract_with_outcome(self, decision):
        self.calls += 1
        return ("llm reason",), "denied"


def _decision(**overrides) -> CaseDecision:
    base = dict(
        case_number="А40-1/2025",
        decision_date=date(2025, 1, 1),
        outcome=CaseOutcome.UNKNOWN,
        reasons=("оценка обстоятельств дела",),
        analysis_text="test",
    )
    base.update(overrides)
    return CaseDecision(**base)


def test_extract_outcome_skips_llm_without_pdf_text():
    llm = _StubLLM()
    extractor = IssueOutcomeExtractor(llm)

    outcome, reasons = asyncio.run(
        extractor.extract_outcome(_decision(pdf_status="not_requested"))
    )

    assert outcome == CaseOutcome.UNKNOWN
    assert reasons == ("оценка обстоятельств дела",)
    assert llm.calls == 0


def test_extract_outcome_uses_rule_based_pdf_result_without_llm():
    llm = _StubLLM()
    extractor = IssueOutcomeExtractor(llm)

    outcome, reasons = asyncio.run(
        extractor.extract_outcome(
            _decision(
                pdf_status="pdf_text",
                proof_quote="...ст. 61.2 Закона о банкротстве...",
                outcome=CaseOutcome.SATISFIED,
                reasons=("неравноценное встречное исполнение",),
            )
        )
    )

    assert outcome == CaseOutcome.SATISFIED
    assert reasons == ("неравноценное встречное исполнение",)
    assert llm.calls == 0
