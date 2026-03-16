from __future__ import annotations

from dataclasses import dataclass

from src.domain.entities import CaseDecision


class SourceRateLimitError(RuntimeError):
    pass


class SourceUnavailableError(RuntimeError):
    pass


class SourceInvalidResponseError(RuntimeError):
    pass


class SourceAccessError(RuntimeError):
    pass


# Aliases for backward compatibility with kad_client.py
KadRateLimitError = SourceRateLimitError
KadUnavailableError = SourceUnavailableError
KadInvalidResponseError = SourceInvalidResponseError
KadAccessError = SourceAccessError


@dataclass
class SearchParams:
    inn_or_name: str | None
    inn_type: str | None
    date_from: str | None
    date_to: str | None
    court: str | None
    case_type: str | None
    case_number: str | None
    article: str | None = None
    full_article: str | None = None
    law_family: str | None = None
    law_display_name: str | None = None
    law_inferred: bool = False
    part: str | None = None
    paragraph: str | None = None
    subparagraph: str | None = None
    issue_phrase: str | None = None
    use_court_filter: bool = True
    # Internal fields for validation
    _regex_quarter: int | None = None


@dataclass(frozen=True)
class FetchStats:
    attempted_cases: int
    successful_cases: int
    retry_count: int
    effective_concurrency: int
    case_id_collection_ms: int
    details_fetch_ms: int
    filtered_by_court: int
    court_compared_cases: int
    court_filter_removed: bool = False
    # Number of decisions dropped by pre-LLM query scope checks
    # (article/paragraph/case-type keyword mismatch in decision text).
    filtered_by_article: int = 0
    # Number of decisions that passed pre-filters but were marked irrelevant by LLM.
    filtered_by_llm_relevance: int = 0
    total_pages: int = 0
    total_cases_found: int = 0


@dataclass(frozen=True)
class FetchDecisionsResult:
    decisions: list[CaseDecision]
    stats: FetchStats
    params: SearchParams | None = None


@dataclass(frozen=True)
class RequestResult:
    data: dict
    retry_count: int
    had_transient_error: bool


@dataclass(frozen=True)
class DecisionFetchOutcome:
    decision: CaseDecision | None
    retry_count: int
    had_transient_error: bool
