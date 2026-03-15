import logging
from typing import Tuple

from src.domain.entities import CaseDecision, CaseOutcome


class IssueOutcomeExtractor:
    """
    Decoupled outcome extractor that prioritizes issue-scoped outcomes
    rather than whole-case generic outcomes (which often represent
    different motions or appeals).
    """

    def __init__(self, llm_extractor=None):
        self._llm = llm_extractor
        self._logger = logging.getLogger("kad.outcome_extractor")

    async def extract_outcome(
        self, decision: CaseDecision
    ) -> Tuple[CaseOutcome, Tuple[str, ...]]:
        """
        Takes a CaseDecision prepopulated by the retrieval engine (Stage A).
        Refines its outcome using the LLM if the outcome is unknown padding.
        """
        current_outcome = decision.outcome

        if (
            decision.pdf_status in {"pdf_text", "ocr_text"}
            and decision.proof_quote
            and current_outcome != CaseOutcome.UNKNOWN
        ):
            return current_outcome, decision.reasons

        if decision.pdf_status not in {"pdf_text", "ocr_text"}:
            return current_outcome, decision.reasons

        # If rule-engine already found a solid SATISFIED/DENIED, trust it for now unless
        # it conflicts heavily with the LLM later, but for speed, we accept rule-based.
        if current_outcome != CaseOutcome.UNKNOWN and not self._llm:
            return current_outcome, decision.reasons

        if self._llm:
            # Re-verify with LLM to ensure the outcome actually applies to the target issue,
            # not just a generic motion in the case.
            llm_reasons, llm_outcome_str = await self._llm.extract_with_outcome(
                decision
            )

            final_outcome = current_outcome
            if llm_outcome_str == "satisfied":
                final_outcome = CaseOutcome.SATISFIED
            elif llm_outcome_str == "denied":
                final_outcome = CaseOutcome.DENIED

            if llm_reasons and llm_reasons != ("оценка обстоятельств дела",):
                return final_outcome, llm_reasons

            return final_outcome, decision.reasons

        return current_outcome, decision.reasons
