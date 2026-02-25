from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum


class CaseOutcome(str, Enum):
    SATISFIED = "satisfied"
    DENIED = "denied"
    UNKNOWN = "unknown"


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
    reason_confidence: float = 1.0  # How confident we are in the extracted reasons (0.0-1.0)
    confidence_score: float = 1.0
    validation_conflicts: tuple[str, ...] = ()


@dataclass(frozen=True)
class AnalysisResult:
    summary: str
    case_list: str
    total_pages: int = 0
    total_cases_found: int = 0
