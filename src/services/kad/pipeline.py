import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

from src.domain.entities import CaseDecision, CaseOutcome, ConfidenceScore, EvidenceTier
from src.domain.kad_models import FetchStats, SearchParams
from src.services.kad.validators.article import ArticleValidator
from src.services.kad.validators.court import JurisdictionValidator
from src.services.kad.validators.outcome import IssueOutcomeExtractor
from src.services.kad.validators.scorer import EvidenceScorer


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


class KadPipeline:
    """
    Two-Stage KAD Pipeline.
    Stage A (Retrieval): Use KadClient to gather candidate case events.
    Stage B (Validation): Run strict validations and assign a Confidence Score.
    """

    def __init__(self, kad_client, llm_reason_extractor=None):
        self.kad_client = kad_client
        self.outcome_extractor = IssueOutcomeExtractor(llm_reason_extractor)
        self.logger = logging.getLogger("kad_pipeline")

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
        retrieval_result = await self.kad_client.fetch_decisions(
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

        article_validator = ArticleValidator(params.article, params.paragraph)
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
            # Since KadClient doesn't do LLM reason extraction anymore, we just use analysis_text
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

            # Apply updates back to the decision for downstream
            decision_kwargs = {**decision.__dict__}
            if final_reasons:
                decision_kwargs["reasons"] = final_reasons
                decision_kwargs["outcome"] = final_outcome

            decision_kwargs["matched_article"] = matched_art
            decision_kwargs["evidence_tier"] = tier
            decision_kwargs["validation_confidence"] = confidence
            decision_kwargs["evidence_quote"] = evidence_quote
            decision_kwargs["confidence_score"] = self._legacy_confidence(confidence)

            decision_copy = decision.__class__(**decision_kwargs)

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
            # If an article is requested, we also count 'Weak' matches because they come from KAD search
            success_thresholds = [ConfidenceScore.CONFIRMED, ConfidenceScore.PROBABLE]
            if params.article:
                success_thresholds.append(ConfidenceScore.WEAK)

            if confidence in success_thresholds:
                validated_count += 1
                if on_successful:
                    on_successful(validated_count)

        self.logger.info(
            f"Pipeline finished. Found {validated_count} strong cases out of {len(candidates)} candidates."
        )

        return PipelineResult(
            validated_records=validated_records, stats=stats, params=params
        )
