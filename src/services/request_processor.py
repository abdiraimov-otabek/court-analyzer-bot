from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime

from src.app.bot_logging import log_event
from src.domain.analysis import AnalysisService
from src.domain.entities import (
    AnalysisResult,
    CaseDecision,
    CaseOutcome,
    ConfidenceScore,
)
from src.domain.kad_models import KadUnavailableError
from src.domain.settings import Settings
from src.domain.value_objects import UserId
from src.infrastructure.cache_repository import AnalysisCacheRepository
from src.infrastructure.log_repository import LogRepository
from src.services.active_requests import ActiveRequestRegistry
from src.services.hashing import HashingService
from src.services.kad.pipeline import KadPipeline
from src.services.kad_client import KadClient
from src.services.query_parser import QueryParser


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
        return QueryMetadata(
            court=court or "суд не указан", period=period, article=article
        )

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
    _CACHE_SCHEMA_VERSION = "v22"

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

    async def process(
        self, user_id: UserId, query_text: str, settings: Settings
    ) -> AnalysisResult:
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

        pipeline = KadPipeline(
            self._kad_client, self._analysis_service._llm_reason_extractor
        )
        fetch_start = datetime.now()
        pipeline_result = await pipeline.run(
            query_text,
            settings,
            on_progress=lambda count: self._active_requests.update_attempted(
                user_id, count
            ),
            on_successful=lambda count: self._active_requests.update_successful(
                user_id, count
            ),
            on_retry=lambda count: self._active_requests.update_retry_count(
                user_id, count
            ),
            on_collection_progress=on_collection_progress,
            on_stage_change=on_stage_change,
            should_cancel=lambda: self._active_requests.is_cancelled(user_id),
        )
        fetch_duration_ms = int((datetime.now() - fetch_start).total_seconds() * 1000)
        validated_records = pipeline_result.validated_records
        decisions = [record.decision for record in validated_records]
        fetch_result_stats = pipeline_result.stats

        self._active_requests.update_attempted(
            user_id, fetch_result_stats.attempted_cases
        )
        self._active_requests.update_successful(
            user_id, fetch_result_stats.successful_cases
        )
        self._active_requests.update_retry_count(
            user_id, fetch_result_stats.retry_count
        )
        if self._active_requests.is_cancelled(user_id):
            raise RequestCancelled()
        if fetch_result_stats.court_filter_removed:
            raise CourtNotFoundError()

        # Determine how many cases were dropped during Stage B Validation
        validated_count = len(decisions)
        dropped_in_validation = fetch_result_stats.successful_cases - validated_count

        # If the user requested an article and cases were dropped in validation, it's an article filtering scenario
        params = pipeline_result.params
        article_filtered = bool(params and params.article and dropped_in_validation > 0)

        min_decisions = 1 if article_filtered else 5
        if validated_count < min_decisions:
            if article_filtered:
                raise NoRelevantCasesError(
                    total_processed=fetch_result_stats.successful_cases,
                    filtered_by_article=fetch_result_stats.filtered_by_article,
                )
            if fetch_result_stats.attempted_cases >= 20:
                # Most cases fetched but filtered by court → court unrecognised by API.
                if (
                    fetch_result_stats.filtered_by_court
                    >= fetch_result_stats.attempted_cases // 2
                ):
                    raise CourtNotFoundError()
                # Many attempted but almost none returned data → transient API outage.
                raise KadUnavailableError(
                    "KAD returned no case data despite large result set"
                )
            raise NotEnoughData()
        article_requested = bool((params.article if params else None) or metadata.article)
        verifiable_records = self._select_verifiable_records(validated_records, article_requested)
        verifiable_decisions = [record.decision for record in verifiable_records]
        satisfied_cases = self._count_outcomes(
            verifiable_decisions, CaseOutcome.SATISFIED
        )
        denied_cases = self._count_outcomes(verifiable_decisions, CaseOutcome.DENIED)
        known_cases = satisfied_cases + denied_cases
        unknown_cases = max(0, len(decisions) - known_cases)
        unknown_share = (unknown_cases / len(decisions)) if decisions else 1.0
        court_mismatch_share = (
            fetch_result_stats.filtered_by_court
            / fetch_result_stats.court_compared_cases
            if fetch_result_stats.court_compared_cases > 0
            else 0.0
        )
        quote_backed_cases = sum(
            1 for record in verifiable_records if self._has_evidence_quote(record.decision)
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

        params = pipeline_result.params
        if params:
            p = params
            # Priority: LLM extracted court > existing metadata (regex)
            metadata = QueryMetadata(
                court=p.court or metadata.court,
                period=metadata.period,
                article=p.article or metadata.article,
            )

            if p.date_from and p.date_to:
                m_from, m_to = int(p.date_from[5:7]), int(p.date_to[5:7])
                year = p.date_from[:4]
                if m_from == 1 and m_to == 3:
                    dp = f"1 квартал {year} года"
                elif m_from == 4 and m_to == 6:
                    dp = f"2 квартал {year} года"
                elif m_from == 7 and m_to == 9:
                    dp = f"3 квартал {year} года"
                elif m_from == 10 and m_to == 12:
                    dp = f"4 квартал {year} года"
                elif m_from == 1 and m_to == 12:
                    dp = f"{year} год"
                else:
                    dp = f"{p.date_from} - {p.date_to}"
                metadata = QueryMetadata(
                    court=metadata.court, period=dp, article=metadata.article
                )

        court_for_summary = metadata.court
        if court_for_summary == "суд не указан" or not court_for_summary:
            known_courts = [
                d.court_name
                for d in decisions
                if d.court_name and d.court_name != "Суд не указан"
            ]
            if known_courts:
                court_for_summary = Counter(known_courts).most_common(1)[0][0]

        # article_requested already calculated above
        quality_reason = self._get_quality_reason(
            known_cases=known_cases,
            unknown_share=unknown_share,
            court_mismatch_share=court_mismatch_share,
            verified_cases=len(verifiable_decisions),
            quote_backed_cases=quote_backed_cases,
            article_requested=article_requested,
            settings=settings,
        )

        build_start = datetime.now()
        result = await self._analysis_service.build_result(
            court=court_for_summary,
            period=metadata.period,
            decisions=decisions,
            article=metadata.article,
            total_pages=fetch_result_stats.total_pages,
            total_cases_found=fetch_result_stats.total_cases_found,
            include_narrative_summary=quality_reason is None,
        )
        build_duration_ms = int((datetime.now() - build_start).total_seconds() * 1000)
        if quality_reason is not None:
            log_event(
                self._logger,
                "analysis.quality_failed",
                reason_code=quality_reason,
                total_cases=len(decisions),
                verified_cases=len(verifiable_decisions),
                known_cases=known_cases,
                unknown_cases=unknown_cases,
                quote_backed_cases=quote_backed_cases,
                unknown_share=round(unknown_share, 4),
                court_mismatch_share=round(court_mismatch_share, 4),
            )
            raise InsufficientQualityError(
                reason_code=quality_reason,
                total_cases=len(decisions),
                verified_cases=len(verifiable_decisions),
                known_cases=known_cases,
                unknown_cases=unknown_cases,
                quote_backed_cases=quote_backed_cases,
                unknown_share=unknown_share,
                court_mismatch_share=court_mismatch_share,
                summary=self._build_quality_warning(
                    reason_code=quality_reason,
                    total_cases=len(decisions),
                    verified_cases=len(verifiable_decisions),
                    known_cases=known_cases,
                    unknown_cases=unknown_cases,
                    quote_backed_cases=quote_backed_cases,
                    article_requested=article_requested,
                ),
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
            attempted_cases=fetch_result_stats.attempted_cases,
            successful_cases=fetch_result_stats.successful_cases,
            retry_count=fetch_result_stats.retry_count,
            effective_concurrency=fetch_result_stats.effective_concurrency,
            case_id_collection_ms=fetch_result_stats.case_id_collection_ms,
            details_fetch_ms=fetch_result_stats.details_fetch_ms,
            filtered_by_court=fetch_result_stats.filtered_by_court,
            court_compared_cases=fetch_result_stats.court_compared_cases,
            fetch_total_ms=fetch_duration_ms,
            result_build_ms=build_duration_ms,
            total_duration_ms=total_duration_ms,
        )
        return result

    def _build_cache_key(self, query_text: str, settings: Settings) -> str:
        raw_key = (
            f"{self._CACHE_SCHEMA_VERSION}|{query_text}|{settings.max_cases}|{settings.max_documents_per_case}|"
            f"{settings.max_pages}|{settings.fetch_concurrency_min}|{settings.fetch_concurrency_max}|"
            f"{settings.analysis_prompt}"
        )
        return hashlib.sha256(raw_key.encode()).hexdigest()

    def _count_outcomes(
        self, decisions: list[CaseDecision], target: CaseOutcome
    ) -> int:
        return sum(
            1 for decision in decisions if self._normalize_outcome(decision) == target
        )

    def _normalize_outcome(self, decision: CaseDecision) -> CaseOutcome:
        return self._analysis_service.normalize_outcome(decision)

    def _get_quality_reason(
        self,
        known_cases: int,
        unknown_share: float,
        court_mismatch_share: float,
        verified_cases: int,
        quote_backed_cases: int,
        article_requested: bool,
        settings: Settings,
    ) -> str | None:
        if verified_cases == 0:
            return "no_verified_cases"
        if article_requested:
            # If an article is specifically requested, we trust KAD's search engine hits
            # and our LLM classification more. We allow summaries even with low 
            # quote-backed percentage, as long as we have at least SOME verified cases.
            if verified_cases > 0:
                return None
            return "no_verified_cases"

        if known_cases < settings.min_known_outcomes:
            return "known_below_threshold"
        if unknown_share >= settings.unknown_outcome_threshold_percent / 100:
            return "unknown_share_high"
                
        if court_mismatch_share >= settings.court_mismatch_threshold_percent / 100:
            return "court_mismatch_high"
        return None

    def _select_verifiable_records(self, validated_records, article_requested: bool):
        allowed_scores = [ConfidenceScore.CONFIRMED, ConfidenceScore.PROBABLE]
        if article_requested:
            allowed_scores.append(ConfidenceScore.WEAK)
            
        return [
            record
            for record in validated_records
            if record.confidence in allowed_scores
        ]

    def _has_evidence_quote(self, decision: CaseDecision) -> bool:
        quote = (decision.evidence_quote or decision.proof_quote).strip()
        if not quote:
            return False
        normalized = quote.lower()
        if normalized in {"n/a", "нет прямой цитаты"}:
            return False
        if normalized.startswith("нет_цитаты"):
            return False
        return True

    def _build_quality_warning(
        self,
        reason_code: str,
        total_cases: int,
        verified_cases: int,
        known_cases: int,
        unknown_cases: int,
        quote_backed_cases: int,
        article_requested: bool,
    ) -> str:
        reason_messages = {
            "no_verified_cases": "Не найдено ни одного подтвержденного судебного акта, который можно безопасно включить в статистику.",
            "no_direct_quotes": "По подтвержденным делам не удалось извлечь ни одной цитаты или фрагмента акта, подтверждающего применение статьи.",
            "known_below_threshold": "Подтвержденных исходов слишком мало для надежной итоговой статистики.",
            "unknown_share_high": "Среди подтвержденных дел слишком велика доля актов без ясного исхода по существу спора.",
            "court_mismatch_high": "Слишком большая доля карточек не совпала с указанным судом.",
        }
        lines = [
            "Качество данных недостаточно для надежной итоговой сводки.",
            f"Обработано карточек: {total_cases}.",
            f"Подтверждено для статистики: {verified_cases}.",
            f"Определенных исходов: {known_cases}.",
            f"Карточек без надежно определенного исхода: {unknown_cases}.",
        ]
        if article_requested:
            lines.append(f"Карточек с цитатой из акта: {quote_backed_cases}.")
        lines.append(
            f"Причина остановки: {reason_messages.get(reason_code, 'данных недостаточно для надежной сводки')}."
        )
        lines.append(
            "Отправляю частичную выгрузку для ручной проверки, но итоговую сводку не формирую."
        )
        return "\n".join(lines)


class RequestCancelled(RuntimeError):
    pass


class NotEnoughData(RuntimeError):
    pass


class CourtNotFoundError(RuntimeError):
    pass


class NoRelevantCasesError(RuntimeError):
    def __init__(self, total_processed: int, filtered_by_article: int) -> None:
        super().__init__("no_relevant_cases")
        self.total_processed = total_processed
        self.filtered_by_article = filtered_by_article


class InsufficientQualityError(RuntimeError):
    def __init__(
        self,
        reason_code: str,
        total_cases: int,
        verified_cases: int,
        known_cases: int,
        unknown_cases: int,
        quote_backed_cases: int,
        unknown_share: float,
        court_mismatch_share: float,
        summary: str,
        case_list: str,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.total_cases = total_cases
        self.verified_cases = verified_cases
        self.known_cases = known_cases
        self.unknown_cases = unknown_cases
        self.quote_backed_cases = quote_backed_cases
        self.unknown_share = unknown_share
        self.court_mismatch_share = court_mismatch_share
        self.summary = summary
        self.case_list = case_list
