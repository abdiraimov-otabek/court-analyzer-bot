from __future__ import annotations

from dataclasses import dataclass

from src.domain.entities import CaseDecision


class KadRateLimitError(RuntimeError):
    pass


class KadUnavailableError(RuntimeError):
    pass


class KadInvalidResponseError(RuntimeError):
    pass


class KadAccessError(RuntimeError):
    pass


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
    paragraph: str | None = None
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
    filtered_by_article: int = 0
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
