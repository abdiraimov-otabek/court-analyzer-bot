import logging
from dataclasses import dataclass, replace
from typing import Callable, List, Optional

from src.domain.entities import CaseDecision, CaseOutcome, ConfidenceScore, EvidenceTier
from src.domain.case_models import FetchStats, SearchParams
from src.services.pipeline.validators.article import ArticleValidator
from src.services.pipeline.validators.court import JurisdictionValidator
from src.services.pipeline.validators.outcome import IssueOutcomeExtractor
from src.services.pipeline.validators.scorer import EvidenceScorer


@dataclass
class ValidationResultRecord:
    decision: CaseDecision
    article_tier: EvidenceTier
    matched_article: str
    evidence_quote: str
    court_match: bool
    issue_outcome: CaseOutcome
    confidence: ConfidenceScore


@dataclass
class PipelineResult:
    validated_records: List[ValidationResultRecord]
    stats: FetchStats
    params: SearchParams


class CasePipeline:
    """
    Two-Stage Case Pipeline.
    Stage A (Retrieval): Use CaseClient to gather candidate case events.
    Stage B (Validation): Run strict validations and assign a Confidence Score.
    """

    def __init__(self, case_client, llm_reason_extractor=None):
        self.case_client = case_client
        self.outcome_extractor = IssueOutcomeExtractor(llm_reason_extractor)
        self.logger = logging.getLogger("case_pipeline")

    @staticmethod
    def _legacy_confidence(confidence: ConfidenceScore) -> float:
        if confidence == ConfidenceScore.CONFIRMED:
            return 1.0
        if confidence == ConfidenceScore.PROBABLE:
            return 0.99
        if confidence == ConfidenceScore.WEAK:
            return 0.5
        return 0.0

    async def run(
        self,
        query_text: str,
        settings,
        on_progress: Optional[Callable[[int], None]] = None,
        on_successful: Optional[Callable[[int], None]] = None,
        on_retry: Optional[Callable[[int], None]] = None,
        on_collection_progress: Optional[Callable[[int], None]] = None,
        on_stage_change: Optional[Callable[[str], None]] = None,
        should_cancel: Optional[Callable[[], bool]] = None,
    ) -> PipelineResult:
        # --- STAGE A: High-Recall Retrieval ---
        # We tell the client to collect cases. It returns FetchDecisionsResult
        # It should no longer run the LLM classification natively.
        self.logger.info("Starting Pipeline Stage A: Retrieval")
        retrieval_result = await self.case_client.fetch_decisions(
            query_text=query_text,
            settings=settings,
            on_progress=on_progress,
            on_successful=None,  # success means validated later
            on_retry=on_retry,
            on_collection_progress=on_collection_progress,
            on_stage_change=on_stage_change,
            should_cancel=should_cancel,
        )

        params = retrieval_result.params
        candidates = retrieval_result.decisions
        stats = retrieval_result.stats

        if not candidates or (should_cancel and should_cancel()):
            return PipelineResult(validated_records=[], stats=stats, params=params)

        # --- STAGE B: High-Precision Validation ---
        self.logger.info(
            f"Starting Pipeline Stage B: Validation on {len(candidates)} candidates"
        )
        if on_stage_change:
            on_stage_change("validating")

        article_validator = ArticleValidator(
            params.article,
            target_paragraph=params.paragraph,
            target_part=getattr(params, "part", None),
            target_subparagraph=getattr(params, "subparagraph", None),
            law_family=getattr(params, "law_family", None),
            law_display_name=getattr(params, "law_display_name", None),
            issue_phrase=getattr(params, "issue_phrase", None),
        )
        validated_records = []

        validated_count = 0
        for decision in candidates:
            if should_cancel and should_cancel():
                break

            # 1. Jurisdiction Validation
            actual_court = decision.court_name
            requested_court = params.court
            court_match = JurisdictionValidator.validate(requested_court, actual_court)

            # 2. Article Validation
            # Since CaseClient doesn't do LLM reason extraction anymore, we just use analysis_text
            text_to_scan = decision.analysis_text or ""
            if params.article:
                tier, matched_art, evidence_quote = article_validator.validate(
                    text_to_scan, llm_proof_quote=decision.proof_quote
                )
            else:
                tier = EvidenceTier.TIER_B_PROBABLE_MATCH
                matched_art = "Статья не запрашивалась"
                evidence_quote = "N/A"

            # 3. Outcome Extraction
            final_outcome, final_reasons = await self.outcome_extractor.extract_outcome(
                decision
            )

            # 4. Confidence Scoring
            confidence = EvidenceScorer.score(tier, court_match, final_outcome)

            decision_copy = replace(
                decision,
                matched_article=matched_art,
                evidence_tier=tier,
                validation_confidence=confidence,
                evidence_quote=evidence_quote,
                confidence_score=self._legacy_confidence(confidence),
                outcome=final_outcome,
                reasons=final_reasons or decision.reasons
            )

            if params.article and confidence == ConfidenceScore.REJECTED:
                failure_code = decision.verification_failure_code or "article_not_confirmed"
                if decision.pdf_status == "pdf_access_blocked":
                    failure_code = "pdf_access_blocked"
                elif decision.pdf_status in {"pdf_unreadable", "pdf_missing"}:
                    failure_code = "pdf_unreadable"
                elif final_outcome == CaseOutcome.UNKNOWN and tier != EvidenceTier.TIER_D_NO_MATCH:
                    failure_code = "outcome_unclear"
                elif decision.decisive_act_type != "merits_act":
                    failure_code = "procedural_only"
                decision_copy = replace(decision_copy, verification_failure_code=failure_code)

            record = ValidationResultRecord(
                decision=decision_copy,
                article_tier=tier,
                matched_article=matched_art,
                evidence_quote=evidence_quote,
                court_match=court_match,
                issue_outcome=final_outcome,
                confidence=confidence,
            )
            validated_records.append(record)

            # Only count 'Confirmed' and 'Probable' as totally successful for the UI progress
            # If an article is requested, we also count 'Weak' matches because they come from search
            success_thresholds = [ConfidenceScore.CONFIRMED, ConfidenceScore.PROBABLE]

            if confidence in success_thresholds:
                validated_count += 1
                if callable(on_successful):
                    on_successful(validated_count)

        self.logger.info(
            f"Pipeline finished. Found {validated_count} strong cases out of {len(candidates)} candidates."
        )

        return PipelineResult(
            validated_records=validated_records, stats=stats, params=params
        )
