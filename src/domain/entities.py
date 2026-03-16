from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class CaseOutcome(str, Enum):
    SATISFIED = "satisfied"
    DENIED = "denied"
    UNKNOWN = "unknown"


class EvidenceTier(str, Enum):
    TIER_A_EXPLICIT_MATCH = "Tier A (Explicit)"
    TIER_B_PROBABLE_MATCH = "Tier B (Probable)"
    TIER_C_WEAK_MATCH = "Tier C (Weak)"
    TIER_D_NO_MATCH = "Tier D (None)"


class ConfidenceScore(str, Enum):
    CONFIRMED = "Confirmed"
    PROBABLE = "Probable"
    WEAK = "Weak"
    REJECTED = "Rejected"


@dataclass(frozen=True)
class CaseDecision:
    case_number: str
    decision_date: date
    outcome: CaseOutcome
    reasons: tuple[str, ...]
    case_id: str = ""
    court_name: str = ""
    case_link: str = ""
    analysis_text: str = ""
    case_category: str = ""  # "Б", "Г", "А", or "" if unknown
    document_links: tuple[dict[str, str], ...] = ()
    proof_quote: str = ""  # Direct citation from the act for verifiable accuracy
    reason_confidence: float = 1.0  # Legacy float confidence
    confidence_score: float = 1.0  # Legacy float confidence accumulator
    validation_conflicts: tuple[str, ...] = ()

    # High-Precision Pipeline Tracking
    matched_article: str = ""
    evidence_quote: str = ""
    evidence_tier: EvidenceTier = EvidenceTier.TIER_D_NO_MATCH
    validation_confidence: ConfidenceScore = ConfidenceScore.REJECTED
    decisive_act_title: str = ""
    decisive_act_url: str = ""
    decisive_act_type: str = ""
    pdf_status: str = "not_requested"
    verification_failure_code: str = ""
    law_display_name: str = ""
    source_system: str = "parser"
    source_quality_score: float = 1.0
    source_quality_reasons: tuple[str, ...] = ()
    extraction_confidence: float = 1.0

    raw_number: str = ""
    raw_date: date | None = None
    raw_case_number: str = ""
    raw_place: str = ""
    raw_judge: str = ""
    raw_url: str = ""
    raw_article: str = ""
    raw_text: str = ""


@dataclass(frozen=True)
class AnalysisResult:
    summary: str
    case_list: str
    total_pages: int = 0
    total_cases_found: int = 0
    primary_source: str = "parser"
    fallback_used: bool = False
    fallback_reason: str = ""
    version_bundle: str = ""
    confidence_score: float = 1.0
    decisions: tuple[CaseDecision, ...] = ()
