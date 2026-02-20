from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from collections import Counter
import sqlite3

from src.domain.analysis import AnalysisService
from src.domain.entities import AnalysisResult, CaseDecision, CaseOutcome
from src.domain.settings import Settings
from src.domain.value_objects import UserId
from src.app.logging import log_event
from src.services.active_requests import ActiveRequestRegistry
from src.services.hashing import HashingService
from src.services.kad_client import KadClient, KadUnavailableError, QueryParser
from src.infrastructure.cache_repository import AnalysisCacheRepository
from src.infrastructure.log_repository import LogRepository


@dataclass(frozen=True)
class QueryMetadata:
    court: str
    period: str
    article: str | None


class QueryMetadataExtractor:
    def __init__(self, parser: QueryParser | None = None) -> None:
        self._parser = parser or QueryParser()

    def extract(self, text: str) -> QueryMetadata:
        year = self._find_year(text)
        quarter = self._find_quarter(text)
        parsed = self._parser.parse(text)
        court = parsed.court
        article = parsed.article
        if year and quarter:
            period = f"{quarter} квартал {year} года"
        elif year:
            period = f"{year} год"
        else:
            period = "период не указан"
        return QueryMetadata(court=court or "суд не указан", period=period, article=article)

    def _find_year(self, text: str) -> str | None:
        for token in text.split():
            if token.isdigit() and len(token) == 4:
                return token
        return None

    def _find_quarter(self, text: str) -> int | None:
        lowered = text.lower()
        if "1 кварт" in lowered or "i кварт" in lowered:
            return 1
        if "2 кварт" in lowered or "ii кварт" in lowered:
            return 2
        if "3 кварт" in lowered or "iii кварт" in lowered:
            return 3
        if "4 кварт" in lowered or "iv кварт" in lowered:
            return 4
        return None


class RequestProcessor:
    _CACHE_SCHEMA_VERSION = "v20"

    def __init__(
        self,
        kad_client: KadClient,
        analysis_service: AnalysisService,
        active_requests: ActiveRequestRegistry,
        cache_repository: AnalysisCacheRepository,
        log_repository: LogRepository,
        hashing_service: HashingService,
        metadata_extractor: QueryMetadataExtractor | None = None,
    ) -> None:
        self._kad_client = kad_client
        self._analysis_service = analysis_service
        self._active_requests = active_requests
        self._cache_repository = cache_repository
        self._log_repository = log_repository
        self._hashing_service = hashing_service
        self._metadata_extractor = metadata_extractor or QueryMetadataExtractor()
        self._logger = logging.getLogger("request_processor")

    async def process(self, user_id: UserId, query_text: str, settings: Settings) -> AnalysisResult:
        if self._active_requests.is_cancelled(user_id):
            raise RequestCancelled()
        total_start = datetime.now()
        log_event(
            self._logger,
            "analysis.started",
            query_text=query_text,
            max_cases=settings.max_cases,
            max_documents_per_case=settings.max_documents_per_case,
            max_pages=settings.max_pages,
        )
        cache_key = self._build_cache_key(query_text, settings)
        now = datetime.now()
        cached = self._cache_repository.get(cache_key, now)
        if cached is not None:
            total_lines = len(cached.case_list.splitlines())
            self._active_requests.update_attempted(user_id, total_lines)
            self._active_requests.update_successful(user_id, total_lines)
            log_event(self._logger, "analysis.cache_hit", cache_key=cache_key)
            return cached

        metadata = self._metadata_extractor.extract(query_text)
        self._active_requests.set_phase(user_id, "collecting")

        _last_collected: list[int] = [0]

        def on_collection_progress(count: int) -> None:
            _last_collected[0] = count
            self._active_requests.update_collected(user_id, count)

        def on_stage_change(phase: str) -> None:
            if phase == "analyzing":
                # Correct the total_cases estimate to the actual collected count
                # so the status message shows "X из X" instead of "X из 500".
                self._active_requests.update_total_cases(user_id, _last_collected[0])
            self._active_requests.set_phase(user_id, phase)
            log_event(self._logger, "analysis.phase", phase=phase)

        fetch_start = datetime.now()
        fetch_result = await self._kad_client.fetch_decisions(
            query_text,
            settings,
            on_progress=lambda count: self._active_requests.update_attempted(user_id, count),
            on_successful=lambda count: self._active_requests.update_successful(user_id, count),
            on_retry=lambda count: self._active_requests.update_retry_count(user_id, count),
            on_collection_progress=on_collection_progress,
            on_stage_change=on_stage_change,
            should_cancel=lambda: self._active_requests.is_cancelled(user_id),
        )
        fetch_duration_ms = int((datetime.now() - fetch_start).total_seconds() * 1000)
        decisions = fetch_result.decisions
        self._active_requests.update_attempted(user_id, fetch_result.stats.attempted_cases)
        self._active_requests.update_successful(user_id, fetch_result.stats.successful_cases)
        self._active_requests.update_retry_count(user_id, fetch_result.stats.retry_count)
        if self._active_requests.is_cancelled(user_id):
            raise RequestCancelled()
        if fetch_result.stats.court_filter_removed:
            raise CourtNotFoundError()
        article_filtered = fetch_result.stats.filtered_by_article > 0
        # After article classification, even 1 relevant case is valid data —
        # the LLM already confirmed relevance, don't reject small sets.
        min_decisions = 1 if article_filtered else 5
        if len(decisions) < min_decisions:
            if article_filtered:
                raise NotEnoughData()
            if fetch_result.stats.attempted_cases >= 20:
                # Most cases fetched but filtered by court → court unrecognised by API.
                if fetch_result.stats.filtered_by_court >= fetch_result.stats.attempted_cases // 2:
                    raise CourtNotFoundError()
                # Many attempted but almost none returned data → transient API outage.
                raise KadUnavailableError("KAD returned no case data despite large result set")
            raise NotEnoughData()
        satisfied_cases = self._count_outcomes(decisions, CaseOutcome.SATISFIED)
        denied_cases = self._count_outcomes(decisions, CaseOutcome.DENIED)
        known_cases = satisfied_cases + denied_cases
        unknown_cases = max(0, len(decisions) - known_cases)
        unknown_share = (unknown_cases / len(decisions)) if decisions else 1.0
        court_mismatch_share = (
            fetch_result.stats.filtered_by_court / fetch_result.stats.court_compared_cases
            if fetch_result.stats.court_compared_cases > 0
            else 0.0
        )
        court_for_summary = metadata.court
        if court_for_summary == "суд не указан":
            known_courts = [
                decision.court_name
                for decision in decisions
                if decision.court_name and decision.court_name != "Суд не указан"
            ]
            if known_courts:
                court_for_summary = Counter(known_courts).most_common(1)[0][0]

        build_start = datetime.now()
        result = self._analysis_service.build_result(court_for_summary, metadata.period, decisions, article=metadata.article)
        build_duration_ms = int((datetime.now() - build_start).total_seconds() * 1000)

        # Skip quality gate when article classification already filtered the set —
        # the remaining cases are intentionally few and quality thresholds designed
        # for 100+ case datasets would reject valid filtered results.
        quality_reason = None if article_filtered else self._get_quality_reason(
            known_cases=known_cases,
            unknown_share=unknown_share,
            court_mismatch_share=court_mismatch_share,
            settings=settings,
        )
        if quality_reason is not None:
            log_event(
                self._logger,
                "analysis.quality_failed",
                reason_code=quality_reason,
                total_cases=len(decisions),
                known_cases=known_cases,
                unknown_cases=unknown_cases,
                unknown_share=round(unknown_share, 4),
                court_mismatch_share=round(court_mismatch_share, 4),
            )
            raise InsufficientQualityError(
                reason_code=quality_reason,
                total_cases=len(decisions),
                known_cases=known_cases,
                unknown_cases=unknown_cases,
                unknown_share=unknown_share,
                court_mismatch_share=court_mismatch_share,
                summary=result.summary,
                case_list=result.case_list,
            )

        try:
            self._cache_repository.set(cache_key, result, now)
        except sqlite3.Error as exc:
            log_event(
                self._logger,
                "analysis.cache_write_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        hashed_user = self._hashing_service.hash_value(user_id.value)
        try:
            self._log_repository.append(hashed_user, query_text, result.summary)
        except sqlite3.Error as exc:
            log_event(
                self._logger,
                "analysis.log_write_failed",
                error_type=type(exc).__name__,
                error=str(exc),
            )
        total_duration_ms = int((datetime.now() - total_start).total_seconds() * 1000)
        log_event(
            self._logger,
            "analysis.completed",
            total_cases=len(decisions),
            attempted_cases=fetch_result.stats.attempted_cases,
            successful_cases=fetch_result.stats.successful_cases,
            retry_count=fetch_result.stats.retry_count,
            effective_concurrency=fetch_result.stats.effective_concurrency,
            case_id_collection_ms=fetch_result.stats.case_id_collection_ms,
            details_fetch_ms=fetch_result.stats.details_fetch_ms,
            filtered_by_court=fetch_result.stats.filtered_by_court,
            court_compared_cases=fetch_result.stats.court_compared_cases,
            fetch_total_ms=fetch_duration_ms,
            result_build_ms=build_duration_ms,
            total_duration_ms=total_duration_ms,
        )
        return result

    def _build_cache_key(self, query_text: str, settings: Settings) -> str:
        return (
            f"{self._CACHE_SCHEMA_VERSION}|{query_text}|{settings.max_cases}|{settings.max_documents_per_case}|"
            f"{settings.max_pages}|{settings.fetch_concurrency_min}|{settings.fetch_concurrency_max}|"
            f"{settings.analysis_prompt}"
        )

    def _count_outcomes(self, decisions: list[CaseDecision], target: CaseOutcome) -> int:
        return sum(1 for decision in decisions if self._normalize_outcome(decision) == target)

    def _normalize_outcome(self, decision: CaseDecision) -> CaseOutcome:
        outcome = decision.outcome
        if isinstance(outcome, CaseOutcome):
            return outcome
        normalized = str(outcome).strip().lower()
        if normalized in {CaseOutcome.SATISFIED.value, "удовлетворено", "удовлетворить"}:
            return CaseOutcome.SATISFIED
        if normalized in {CaseOutcome.DENIED.value, "отказано", "отказать"}:
            return CaseOutcome.DENIED
        return CaseOutcome.UNKNOWN

    def _get_quality_reason(
        self,
        known_cases: int,
        unknown_share: float,
        court_mismatch_share: float,
        settings: Settings,
    ) -> str | None:
        if known_cases < settings.min_known_outcomes:
            return "known_below_threshold"
        if unknown_share >= settings.unknown_outcome_threshold_percent / 100:
            return "unknown_share_high"
        if court_mismatch_share >= settings.court_mismatch_threshold_percent / 100:
            return "court_mismatch_high"
        return None


class RequestCancelled(RuntimeError):
    pass


class NotEnoughData(RuntimeError):
    pass


class CourtNotFoundError(RuntimeError):
    pass


class InsufficientQualityError(RuntimeError):
    def __init__(
        self,
        reason_code: str,
        total_cases: int,
        known_cases: int,
        unknown_cases: int,
        unknown_share: float,
        court_mismatch_share: float,
        summary: str,
        case_list: str,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.total_cases = total_cases
        self.known_cases = known_cases
        self.unknown_cases = unknown_cases
        self.unknown_share = unknown_share
        self.court_mismatch_share = court_mismatch_share
        self.summary = summary
        self.case_list = case_list
