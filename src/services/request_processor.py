from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime

from src.app.bot_logging import log_event
from src.domain.analysis import AnalysisService
from src.domain.entities import (
    AnalysisResult,
    CaseDecision,
    CaseOutcome,
    ConfidenceScore,
)
from src.domain.case_models import SourceUnavailableError, CaseClient
from src.domain.settings import Settings
from src.domain.value_objects import UserId
from src.domain.versioning import current_version_bundle
from src.infrastructure.cache_repository import AnalysisCacheRepository
from src.infrastructure.log_repository import LogRepository
from src.services.active_requests import ActiveRequestRegistry
from src.services.hashing import HashingService
from src.services.pipeline.pipeline import CasePipeline
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
        article = parsed.full_article or parsed.article
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
    _CACHE_SCHEMA_VERSION = "v24"

    def __init__(
        self,
        case_client: CaseClient,
        analysis_service: AnalysisService,
        active_requests: ActiveRequestRegistry,
        cache_repository: AnalysisCacheRepository,
        log_repository: LogRepository,
        hashing_service: HashingService,
        metadata_extractor: QueryMetadataExtractor | None = None,
    ) -> None:
        self._case_client = case_client
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
            return replace(cached, version_bundle=current_version_bundle())

        metadata = self._metadata_extractor.extract(query_text)
        self._active_requests.set_phase(user_id, "fetching_cases")

        _last_collected: list[int] = [0]

        def on_collection_progress(count: int) -> None:
            capped_count = min(count, settings.max_cases)
            _last_collected[0] = capped_count
            self._active_requests.update_collected(user_id, capped_count)

        def on_stage_change(phase: str) -> None:
            self._active_requests.set_phase(user_id, phase)
            log_event(self._logger, "analysis.phase", phase=phase)

        pipeline = CasePipeline(
            self._case_client, self._analysis_service._llm_reason_extractor
        )
        fetch_start = datetime.now()
        pipeline_result = await pipeline.run(
            query_text,
            settings,
            on_progress=lambda count: self._active_requests.update_attempted(
                user_id, min(count, settings.max_cases)
            ),
            on_successful=lambda count: self._active_requests.update_successful(
                user_id, min(count, settings.max_cases)
            ),
            on_retry=lambda count: self._active_requests.update_retry_count(
                user_id, count
            ),
            on_collection_progress=on_collection_progress,
            on_stage_change=on_stage_change,
            should_cancel=lambda: self._active_requests.is_cancelled(user_id),
        )
        fetch_duration_ms = int((datetime.now() - fetch_start).total_seconds() * 1000)
        validated_records = pipeline_result.validated_records[: settings.max_cases]
        decisions = [
            self._truncate_analysis_text(record.decision, settings.max_analysis_text_length)
            for record in validated_records
        ]
        fetch_result_stats = pipeline_result.stats

        self._active_requests.update_attempted(
            user_id, min(fetch_result_stats.attempted_cases, settings.max_cases)
        )
        self._active_requests.update_successful(
            user_id, min(fetch_result_stats.successful_cases, settings.max_cases)
        )
        self._active_requests.update_retry_count(
            user_id, fetch_result_stats.retry_count
        )
        if self._active_requests.is_cancelled(user_id):
            raise RequestCancelled()

        quality_reason: str | None = None
        if fetch_result_stats.court_filter_removed:
            quality_reason = "court_not_found"

        # Determine how many cases were dropped during Stage B Validation
        validated_count = len(decisions)
        dropped_in_validation = fetch_result_stats.successful_cases - validated_count

        # If the user requested an article and cases were dropped in validation, it's an article filtering scenario
        params = pipeline_result.params
        article_filtered = bool(params and params.article and dropped_in_validation > 0)

        min_decisions = 1 if article_filtered else settings.min_known_outcomes
        if not quality_reason and validated_count < min_decisions:
            if article_filtered:
                quality_reason = "no_relevant_cases"
            elif fetch_result_stats.attempted_cases >= 20:
                if fetch_result_stats.filtered_by_court >= fetch_result_stats.attempted_cases // 2:
                    quality_reason = "court_not_found"
                else:
                    quality_reason = "source_unavailable"
            else:
                quality_reason = "not_enough_data"
        article_requested = bool((params.article if params else None) or metadata.article)
        verifiable_records = self._select_verifiable_records(validated_records)
        verifiable_decisions = [record.decision for record in verifiable_records]
        satisfied_cases = self._count_outcomes(decisions, CaseOutcome.SATISFIED)
        denied_cases = self._count_outcomes(decisions, CaseOutcome.DENIED)
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

        court_for_summary = self._resolve_court_for_summary(metadata.court, decisions)

        # article_requested already calculated above
        if quality_reason is None:
            quality_reason = self._get_quality_reason(
                known_cases=known_cases,
                unknown_share=unknown_share,
                court_mismatch_share=court_mismatch_share,
                verified_cases=len(verifiable_decisions),
                quote_backed_cases=quote_backed_cases,
                article_requested=article_requested,
                settings=settings,
            )

        self._active_requests.set_phase(user_id, "analyzing")
        build_start = datetime.now()
        # Use attempted_cases for total_cases_found so the summary exactly matches the progress counter
        visible_total_cases_found = min(
            fetch_result_stats.attempted_cases or fetch_result_stats.total_cases_found or len(decisions),
            settings.max_cases,
        )
        result = await self._analysis_service.build_result(
            court=court_for_summary,
            period=metadata.period,
            decisions=decisions,
            article=metadata.article,
            total_pages=fetch_result_stats.total_pages,
            total_cases_found=visible_total_cases_found,
            include_narrative_summary=True,
            model_override=settings.llm_model,
        )
        build_duration_ms = int((datetime.now() - build_start).total_seconds() * 1000)

        if not decisions:
            if quality_reason == "no_relevant_cases":
                raise NoRelevantCasesError(
                    total_processed=fetch_result_stats.attempted_cases,
                    filtered_by_article=fetch_result_stats.filtered_by_article or 0,
                )
            if quality_reason == "court_not_found":
                raise CourtNotFoundError()
            if quality_reason in ("not_enough_data", "source_unavailable"):
                raise NotEnoughData()

        if quality_reason is not None:
            log_event(
                self._logger,
                "analysis.quality_warning_ignored",
                reason_code=quality_reason,
                total_cases=len(decisions),
                verified_cases=len(verifiable_decisions),
                known_cases=known_cases,
            )

        self._active_requests.set_phase(user_id, "aggregating")
        primary_source, fallback_used, fallback_reason = self._summarize_sources(
            decisions
        )
        confidence_score = self._compute_request_confidence(
            decisions=decisions,
            verified_cases=len(verifiable_decisions),
            article_requested=article_requested,
        )
        result = replace(
            result,
            primary_source=primary_source,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            version_bundle=current_version_bundle(),
            confidence_score=confidence_score,
            summary=self._append_llm_status_note(
                result.summary,
                self._analysis_service._llm_reason_extractor
            )
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
            primary_source=primary_source,
            fallback_used=fallback_used,
            fallback_reason=fallback_reason,
            confidence_score=round(confidence_score, 4),
        )
        return result

    def _build_cache_key(self, query_text: str, settings: Settings) -> str:
        raw_key = (
            f"{self._CACHE_SCHEMA_VERSION}|{query_text}|{settings.max_cases}|{settings.max_documents_per_case}|"
            f"{settings.max_pages}|{settings.max_llm_calls_per_request}|{settings.max_analysis_text_length}|"
            f"{settings.fetch_concurrency_min}|{settings.fetch_concurrency_max}|"
            f"{current_version_bundle()}|"
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
            if quote_backed_cases == 0:
                return "no_direct_quotes"

        if known_cases < settings.min_known_outcomes:
            return "known_below_threshold"
        if unknown_share >= settings.unknown_outcome_threshold_percent / 100:
            return "unknown_share_high"
                
        if court_mismatch_share >= settings.court_mismatch_threshold_percent / 100:
            return "court_mismatch_high"
        return None

    def _select_verifiable_records(self, validated_records):
        allowed_scores = [
            ConfidenceScore.CONFIRMED,
            ConfidenceScore.PROBABLE,
            ConfidenceScore.WEAK,
        ]
        return [
            record
            for record in validated_records
            if record.confidence in allowed_scores
        ]

    def _resolve_court_for_summary(
        self, extracted_court: str, decisions: list[CaseDecision]
    ) -> str:
        if extracted_court and extracted_court != "суд не указан":
            return extracted_court
        known_courts = [
            decision.court_name
            for decision in decisions
            if decision.court_name and decision.court_name != "Суд не указан"
        ]
        if not known_courts:
            return extracted_court
        return Counter(known_courts).most_common(1)[0][0]

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
            "court_not_found": "По вашему запросу не найдено дел в указанном суде. Возможно, название указано неточно или суд не поддерживается.",
            "no_relevant_cases": "По выбранной статье/тематике в указанном периоде не найдено релевантных судебных актов.",
            "not_enough_data": "Найдено слишком мало дел (менее 5). Недостаточно данных для статистического анализа.",
            "source_unavailable": "Источник данных вернул пустой результат при большом объеме найденных дел. Возможен временный сбой.",
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
        
        reason_msg = reason_messages.get(reason_code, "данных недостаточно для надежной сводки")
        lines.append(f"Причина остановки: {reason_msg.rstrip('.')}")
        
        if total_cases > 0:
            lines.append("Отправляю частичную выгрузку для ручной проверки, но итоговую сводку не формирую.")
        else:
            lines.append("Итоговая сводка и выгрузка не сформированы.")
            
        return "\n".join(lines)

    def _summarize_sources(
        self, decisions: list[CaseDecision]
    ) -> tuple[str, bool, str]:
        if not decisions:
            return "parser", False, ""
        sources = Counter(
            (decision.source_system or "parser") for decision in decisions
        )
        primary_source = sources.most_common(1)[0][0]
        fallback_used = len(sources) > 1 or any(
            "fallback" in reason.lower()
            for decision in decisions
            for reason in decision.source_quality_reasons
        )
        fallback_reason = "mixed_sources" if fallback_used else ""
        return primary_source, fallback_used, fallback_reason

    def _compute_request_confidence(
        self,
        decisions: list[CaseDecision],
        verified_cases: int,
        article_requested: bool,
    ) -> float:
        if not decisions:
            return 0.0
        source_quality = sum(d.source_quality_score for d in decisions) / len(decisions)
        document_count_score = min(len(decisions) / max(verified_cases, 1), 1.0)
        article_match_rate = (
            verified_cases / len(decisions) if article_requested else 1.0
        )
        extraction_confidence = (
            sum(d.extraction_confidence for d in decisions) / len(decisions)
        )
        fallback_ratio = sum(
            1 for d in decisions if (d.source_system or "parser") != "ras"
        ) / len(decisions)
        confidence = (
            0.3 * source_quality
            + 0.2 * document_count_score
            + 0.2 * article_match_rate
            + 0.2 * extraction_confidence
            + 0.1 * (1.0 - fallback_ratio)
        )
        return max(0.0, min(confidence, 1.0))

    def _append_reliability_note(
        self, summary: str, fallback_used: bool, fallback_reason: str
    ) -> str:
        if not fallback_used:
            return summary
        note = "Часть результата построена с использованием резервного источника."
        if fallback_reason == "semantic_fallback":
            note = (
                "Часть результата построена с использованием резервного источника "
                "из-за недостаточного качества исходного текста."
            )
        return f"{summary}\n\nПримечание: {note}"

    def _append_llm_status_note(self, summary: str, extractor) -> str:
        if extractor is None:
            return summary + "\n\n⚠️ AI-анализ отключен (отсутствует API ключ)."
        if not extractor.is_functional:
             error_note = ""
             if "402" in (extractor.last_error or ""):
                 error_note = " (недостаточно средств на балансе AI-провайдера)"
             return summary + f"\n\n⚠️ AI-анализ временно ограничен{error_note}. Выводы могут быть менее точными."
        return summary

    def _truncate_analysis_text(
        self, decision: CaseDecision, max_length: int
    ) -> CaseDecision:
        if len(decision.analysis_text) <= max_length:
            return decision
        return replace(decision, analysis_text=decision.analysis_text[:max_length])


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
        decisions: tuple[CaseDecision, ...],
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
        self.decisions = decisions
