from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from dataclasses import replace
from datetime import date, datetime
from typing import Callable, Protocol
from urllib.parse import quote_plus

import httpx

from src.app.bot_logging import log_debug, log_event
from src.domain.entities import CaseDecision, CaseOutcome
from src.domain.kad_models import (
    DecisionFetchOutcome,
    FetchDecisionsResult,
    FetchStats,
    KadAccessError,
    KadInvalidResponseError,
    KadRateLimitError,
    KadUnavailableError,
    RequestResult,
    SearchParams,
)
from src.domain.reason_extractor import ReasonExtractor
from src.domain.settings import Settings
from src.infrastructure.case_details_cache_repository import CaseDetailsCacheRepository
from src.services.llm_reason_extractor import LLMReasonExtractor
from src.services.query_parser import QueryParser


class KadClient(Protocol):
    def count_cases(self, query_text: str, settings: Settings) -> int: ...

    async def count_cases_async(self, query_text: str, settings: Settings) -> int: ...

    async def fetch_decisions(
        self,
        query_text: str,
        settings: Settings,
        on_progress: Callable[[int], None] | None = None,
        on_successful: Callable[[int], None] | None = None,
        on_retry: Callable[[int], None] | None = None,
        on_collection_progress: Callable[[int], None] | None = None,
        on_stage_change: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> FetchDecisionsResult: ...


class ParserApiKadClient:
    _MAX_UNKNOWN_OUTCOME_ENRICHMENT = 120

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout_seconds: int = 30,
        details_by_id_path: str = "/details_by_id",
        details_by_number_path: str = "/details_by_number",
        search_path: str = "/search",
        sync_http_client: httpx.Client | None = None,
        async_http_client: httpx.AsyncClient | None = None,
        details_cache_repository: CaseDetailsCacheRepository | None = None,
        llm_reason_extractor: LLMReasonExtractor | None = None,
    ) -> None:
        if not base_url:
            raise ValueError("base_url is required")
        if not api_key:
            raise ValueError("api_key is required")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout = timeout_seconds
        self._details_by_id_path = details_by_id_path
        self._details_by_number_path = details_by_number_path
        self._search_path = search_path
        self._parser = QueryParser()
        self._reason_extractor = ReasonExtractor()
        self._llm_reason_extractor = llm_reason_extractor
        self._logger = logging.getLogger("kad_client")
        # Cache court-name → accepted(True) / rejected(False) to skip redundant probe calls.
        self._court_validity_cache: dict[str, bool] = {}
        # Fields captured from search results (search API includes them; details_by_id does not).
        self._search_case_types: dict[str, str] = {}
        self._search_case_numbers: dict[str, str] = {}
        self._details_cache_repository = details_cache_repository
        limits = httpx.Limits(max_connections=100, max_keepalive_connections=50)
        self._sync_http_client = sync_http_client or httpx.Client(
            timeout=self._timeout, limits=limits
        )
        self._async_http_client = async_http_client or httpx.AsyncClient(
            timeout=self._timeout, limits=limits
        )
        self._owns_sync_client = sync_http_client is None
        self._owns_async_client = async_http_client is None
        self._current_article: str | None = None

    async def aclose(self) -> None:
        if self._owns_async_client:
            await self._async_http_client.aclose()
        if self._owns_sync_client:
            self._sync_http_client.close()

    def count_cases(self, query_text: str, settings: Settings) -> int:
        params = self._parser.parse(query_text)
        if params.case_number:
            case = self._fetch_case_by_number(
                params.case_number, settings.max_documents_per_case
            )
            return 1 if case else 0
        params = self._sanitize_params(params)
        first_page = self._search(params, page=1)
        pages_count = int(first_page.get("PagesCount", 0) or 0)
        first_cases = first_page.get("Cases", [])
        count = len(first_cases)
        if count == 0:
            return 0
        if pages_count <= 1:
            return count

        page_size = len(first_cases)
        estimated_total = pages_count * max(page_size, 1)
        if estimated_total > settings.max_cases:
            return settings.max_cases + 1

        max_page = min(settings.max_pages, pages_count)
        for page in range(2, max_page + 1):
            data = self._search(params, page)
            cases = data.get("Cases", [])
            count += len(cases)
            if count > settings.max_cases:
                return count
        if pages_count > settings.max_pages and count >= settings.max_cases:
            return settings.max_cases + 1
        return count

    async def count_cases_async(self, query_text: str, settings: Settings) -> int:
        """Async version of count_cases — fetches remaining pages in parallel."""
        params = self._parser.parse(query_text)
        params = await self._refine_params_with_llm(query_text, params)

        if params.case_number:
            case = await self._fetch_case_by_number_async(
                params.case_number, settings.max_documents_per_case
            )
            return 1 if case else 0
        params = await self._sanitize_params_async(params)

        # First page — needed to get PagesCount before parallelising
        data_result = await self._request_json_async(
            "GET", self._search_path, params=self._build_query(params, page=1)
        )
        data = self._validate_success(data_result.data)
        pages_count = int(data.get("PagesCount", 0) or 0)
        first_cases = data.get("Cases", [])
        count = len(first_cases)

        if count == 0:
            return 0
        if pages_count <= 1:
            return count

        page_size = max(len(first_cases), 1)
        estimated_total = pages_count * page_size
        if estimated_total > settings.max_cases:
            return settings.max_cases + 1

        # Fetch all remaining pages simultaneously
        max_page = min(settings.max_pages, pages_count)
        if max_page <= 1:
            return count

        page_tasks = [
            asyncio.create_task(
                self._request_json_async(
                    "GET", self._search_path, params=self._build_query(params, page=p)
                )
            )
            for p in range(2, max_page + 1)
        ]
        results = await asyncio.gather(*page_tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                continue  # best-effort: skip failed pages
            try:
                page_data = self._validate_success(result.data)
                count += len(page_data.get("Cases", []))
                if count > settings.max_cases:
                    return count
            except Exception:
                continue

        if pages_count > settings.max_pages and count >= settings.max_cases:
            return settings.max_cases + 1
        return count

    async def fetch_decisions(
        self,
        query_text: str,
        settings: Settings,
        on_progress: Callable[[int], None] | None = None,
        on_successful: Callable[[int], None] | None = None,
        on_retry: Callable[[int], None] | None = None,
        on_collection_progress: Callable[[int], None] | None = None,
        on_stage_change: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> FetchDecisionsResult:
        params = self._parser.parse(query_text)
        params = await self._refine_params_with_llm(query_text, params)
        if self._llm_reason_extractor is not None:
            # Issue #5: Reset before setting to ensure clean slate per request
            self._llm_reason_extractor.reset_fetch_budget()
            # When article is specified, every case needs classification — raise budget
            self._llm_reason_extractor.set_fetch_budget(max_calls=600)
        if should_cancel and should_cancel():
            return self._empty_fetch_result()
        if params.case_number:
            if on_stage_change:
                on_stage_change("analyzing")
            outcome = await self._fetch_case_by_number_async(
                params.case_number, settings.max_documents_per_case
            )
            decisions = [outcome] if outcome else []
            return FetchDecisionsResult(
                decisions=decisions,
                stats=FetchStats(
                    attempted_cases=1 if params.case_number else 0,
                    successful_cases=len(decisions),
                    retry_count=0,
                    effective_concurrency=1,
                    case_id_collection_ms=0,
                    details_fetch_ms=0,
                    filtered_by_court=0,
                    court_compared_cases=0,
                    total_pages=1,
                    total_cases_found=1,
                ),
            )

        if on_stage_change:
            on_stage_change("collecting")
        collect_start = time.perf_counter()
        params = await self._sanitize_params_async(params)

        # If the API rejected the Court filter, the search would return cases from
        # all courts — fetching their details would waste hundreds of API calls and
        # yield 0 useful results. Fail fast only when the court was *definitively*
        # rejected (HTTP 400, cached False). A Success=0 probe result means the
        # court name is syntactically valid but the combination has no results;
        # in that case we proceed without the API court filter and rely on local
        # court_name matching inside fetch to narrow results down.
        if (
            params.court
            and not params.use_court_filter
            and self._court_validity_cache.get(params.court) is False
        ):
            log_event(
                self._logger, "fetch_decisions.court_filter_removed", court=params.court
            )
            return FetchDecisionsResult(
                decisions=[],
                stats=FetchStats(
                    attempted_cases=0,
                    successful_cases=0,
                    retry_count=0,
                    effective_concurrency=0,
                    case_id_collection_ms=0,
                    details_fetch_ms=0,
                    filtered_by_court=0,
                    court_compared_cases=0,
                    court_filter_removed=True,
                ),
            )

        case_ids, pages_count = await self._collect_case_ids(
            params,
            settings,
            should_cancel=should_cancel,
            on_collection_progress=on_collection_progress,
        )
        case_id_collection_ms = int((time.perf_counter() - collect_start) * 1000)

        attempted_cases = 0
        successful_cases = 0
        retry_count = 0
        filtered_by_court = 0
        filtered_by_article = 0
        filtered_by_llm_relevance = 0
        court_compared_cases = 0
        decisions: list[CaseDecision] = []
        details_start = time.perf_counter()
        unavailable_failures = 0
        rate_limit_failures = 0
        invalid_failures = 0
        expected_court_tokens = self._court_tokens(params.court)

        min_concurrency = settings.fetch_concurrency_min
        max_concurrency = settings.fetch_concurrency_max
        target_concurrency = min(max_concurrency, max(min_concurrency, 8))
        stable_success_window = 0
        case_limit = min(settings.max_cases, len(case_ids))
        next_index = 0
        in_flight: set[asyncio.Task[DecisionFetchOutcome]] = set()

        if on_stage_change:
            on_stage_change("analyzing")

        self._current_article = params.article
        try:

            async def _fill_in_flight() -> None:
                nonlocal next_index
                while (
                    next_index < case_limit
                    and len(in_flight) < target_concurrency
                    and not (should_cancel and should_cancel())
                ):
                    in_flight.add(
                        asyncio.create_task(
                            self._fetch_case_decision_with_metrics(
                                case_ids[next_index], settings
                            )
                        )
                    )
                    next_index += 1

            await _fill_in_flight()
            while in_flight:
                if should_cancel and should_cancel():
                    for task in in_flight:
                        task.cancel()
                    await asyncio.gather(*in_flight, return_exceptions=True)
                    break

                done, _ = await asyncio.wait(
                    in_flight, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    in_flight.discard(task)
                    attempted_cases += 1
                    if on_progress:
                        on_progress(attempted_cases)
                    try:
                        outcome = await task
                    except KadUnavailableError:
                        unavailable_failures += 1
                        target_concurrency = max(
                            min_concurrency, target_concurrency - 2
                        )
                        stable_success_window = 0
                        log_event(
                            self._logger,
                            "fetch_decisions.case_failed",
                            error_type="KadUnavailableError",
                            unavailable_failures=unavailable_failures,
                            attempted_cases=attempted_cases,
                        )
                        continue
                    except KadRateLimitError:
                        rate_limit_failures += 1
                        target_concurrency = max(
                            min_concurrency, target_concurrency - 2
                        )
                        stable_success_window = 0
                        log_event(
                            self._logger,
                            "fetch_decisions.case_failed",
                            error_type="KadRateLimitError",
                            rate_limit_failures=rate_limit_failures,
                            attempted_cases=attempted_cases,
                        )
                        continue
                    except KadInvalidResponseError:
                        invalid_failures += 1
                        log_event(
                            self._logger,
                            "fetch_decisions.case_failed",
                            error_type="KadInvalidResponseError",
                            invalid_failures=invalid_failures,
                            attempted_cases=attempted_cases,
                        )
                        continue
                    if outcome.decision is not None:
                        decision = outcome.decision
                        if not self._matches_query_scope(decision, params):
                            filtered_by_article += 1
                            continue
                        if expected_court_tokens:
                            actual_court_tokens = self._court_tokens(
                                decision.court_name
                            )
                            if actual_court_tokens:
                                court_compared_cases += 1
                            if actual_court_tokens and not self._courts_match(
                                expected_court_tokens, actual_court_tokens
                            ):
                                filtered_by_court += 1
                                continue
                            if not actual_court_tokens and params.court:
                                decision = replace(decision, court_name=params.court)
                        decisions.append(decision)
                        successful_cases += 1
                        if on_successful:
                            on_successful(successful_cases)
                    if outcome.retry_count:
                        retry_count += outcome.retry_count
                        if on_retry:
                            on_retry(retry_count)

                    if outcome.had_transient_error:
                        target_concurrency = max(
                            min_concurrency, target_concurrency - 2
                        )
                        stable_success_window = 0
                    else:
                        stable_success_window += 1
                        if (
                            stable_success_window >= target_concurrency
                            and target_concurrency < max_concurrency
                        ):
                            target_concurrency += 1
                            stable_success_window = 0

                    if attempted_cases % 25 == 0:
                        log_event(
                            self._logger,
                            "fetch_decisions.progress",
                            attempted_cases=attempted_cases,
                            successful_cases=successful_cases,
                            retry_count=retry_count,
                            target_concurrency=target_concurrency,
                            in_flight=len(in_flight),
                        )

                    if attempted_cases >= case_limit:
                        for pending in in_flight:
                            pending.cancel()
                        await asyncio.gather(*in_flight, return_exceptions=True)
                        in_flight.clear()
                        break
                await _fill_in_flight()
        finally:
            self._current_article = None
            if in_flight:
                for task in in_flight:
                    task.cancel()
                await asyncio.gather(*in_flight, return_exceptions=True)
                in_flight.clear()

        details_fetch_ms = int((time.perf_counter() - details_start) * 1000)
        log_event(
            self._logger,
            "fetch_decisions.stats",
            attempted_cases=attempted_cases,
            successful_cases=successful_cases,
            retry_count=retry_count,
            effective_concurrency=target_concurrency,
            case_id_collection_ms=case_id_collection_ms,
            details_fetch_ms=details_fetch_ms,
            unavailable_failures=unavailable_failures,
            rate_limit_failures=rate_limit_failures,
            invalid_failures=invalid_failures,
            filtered_by_court=filtered_by_court,
            court_compared_cases=court_compared_cases,
        )
        if successful_cases == 0:
            if rate_limit_failures > 0:
                raise KadRateLimitError("KAD API rate limit exceeded")
            if unavailable_failures > 0:
                raise KadUnavailableError("KAD API unavailable")
            if invalid_failures > 0:
                raise KadInvalidResponseError("KAD API request failed")

        # When article is specified, first hard-filter by case category, then classify via LLM
        if params.article and decisions:
            pre_filter_count = len(decisions)
            category_filtered = 0
            # Removed the aggressive 'Б' case category filter.
            # Bankruptcy adversarial proceedings (complaints, invalidations under art. 60/61)
            # are frequently classified as 'Г' (Civil) by the KAD system. Filtering them
            # out here causes massive false negatives.
            pre_llm_count = len(decisions)

        if params.article and self._llm_reason_extractor is not None and decisions:
            if on_stage_change:
                on_stage_change("classifying")
            log_event(
                self._logger,
                "fetch_decisions.article_classification_start",
                article=params.article,
                total_decisions=len(decisions),
            )

            # Batch classification for 100% data quality and cost reduction
            classify_results = await self._llm_reason_extractor.classify_batch(
                decisions, params.article, query_text
            )

            source_decisions = decisions
            relevant_decisions: list[CaseDecision] = []
            filtered_by_relevance = 0
            no_quote_prefix = self._llm_reason_extractor._NO_QUOTE_PREFIX
            for idx, decision in enumerate(source_decisions):
                if idx < len(classify_results):
                    is_relevant, reasons, proof_quote, llm_outcome = classify_results[idx]
                else:
                    # Defensive fallback: keep the case if classification result is missing.
                    is_relevant, reasons, proof_quote, llm_outcome = (
                        True,
                        ("оценка обстоятельств дела", "LLM batch result missing"),
                        "",
                        None,
                    )
                if is_relevant:
                    # Determine reason_confidence based on proof_quote quality
                    if proof_quote and not proof_quote.startswith(no_quote_prefix):
                        reason_conf = 1.0
                    else:
                        reason_conf = 0.7

                    updated = replace(
                        decision, reasons=reasons, proof_quote=proof_quote,
                        reason_confidence=reason_conf,
                    )

                    confidence = 1.0
                    conflicts = []

                    if llm_outcome:
                        if updated.outcome == CaseOutcome.UNKNOWN:
                            # Pure LLM reliance (no regex backing) drops confidence safely
                            confidence -= 0.05
                            if llm_outcome == "satisfied":
                                updated = replace(
                                    updated, outcome=CaseOutcome.SATISFIED
                                )
                            elif llm_outcome == "denied":
                                updated = replace(updated, outcome=CaseOutcome.DENIED)
                        else:
                            # Rule-based detection succeeded. Verify LLM matches.
                            if updated.outcome.value != llm_outcome:
                                confidence -= 1.0
                                conflicts.append(
                                    f"Conflict: Rules engine detected '{updated.outcome.value}' but LLM extracted '{llm_outcome}'"
                                )

                    updated = replace(
                        updated,
                        confidence_score=max(0.0, confidence),
                        validation_conflicts=tuple(conflicts),
                    )
                    relevant_decisions.append(updated)
                else:
                    filtered_by_relevance += 1

            decisions = relevant_decisions
            successful_cases = len(decisions)

            # Safety net: if LLM rejected every pre-filtered case for a specific article,
            # fall back to deterministic article-scope matches instead of returning a
            # misleading "0 релевантных" for large batches.
            if pre_llm_count > 0 and not decisions:
                log_event(
                    self._logger,
                    "fetch_decisions.article_classification_all_rejected_fallback",
                    article=params.article,
                    pre_llm_count=pre_llm_count,
                )
                decisions = [
                    replace(
                        d,
                        reasons=(
                            "совпадение по статье",
                            "fallback: deterministic article match",
                        ),
                        reason_confidence=0.65,
                    )
                    for d in source_decisions
                ]
                successful_cases = len(decisions)
                filtered_by_relevance = 0

            # Update the global counter for the final stats object
            filtered_by_llm_relevance = filtered_by_relevance

            log_event(
                self._logger,
                "fetch_decisions.article_classification_done",
                article=params.article,
                total_input=pre_filter_count,
                pre_llm_count=pre_llm_count,
                relevant=len(decisions),
                filtered_category=category_filtered,
                filtered_relevance=filtered_by_relevance,
            )

            decisions = await self._enrich_unknown_outcomes_with_llm(
                decisions, should_cancel=should_cancel
            )
            successful_cases = len(decisions)

        if self._llm_reason_extractor is not None:
            self._llm_reason_extractor.reset_fetch_budget()
        self._current_article = None
        return FetchDecisionsResult(
            decisions=decisions,
            stats=FetchStats(
                attempted_cases=attempted_cases,
                successful_cases=successful_cases,
                retry_count=retry_count,
                effective_concurrency=target_concurrency,
                case_id_collection_ms=case_id_collection_ms,
                details_fetch_ms=details_fetch_ms,
                filtered_by_court=filtered_by_court,
                court_compared_cases=court_compared_cases,
                filtered_by_article=filtered_by_article,
                filtered_by_llm_relevance=filtered_by_llm_relevance,
                total_pages=pages_count,
                total_cases_found=len(case_ids),
            ),
            params=params,
        )

    def _empty_fetch_result(self) -> FetchDecisionsResult:
        return FetchDecisionsResult(
            decisions=[],
            stats=FetchStats(
                attempted_cases=0,
                successful_cases=0,
                retry_count=0,
                effective_concurrency=0,
                case_id_collection_ms=0,
                details_fetch_ms=0,
                filtered_by_court=0,
                court_compared_cases=0,
            ),
            params=None,
        )

    async def _collect_case_ids(
        self,
        params: SearchParams,
        settings: Settings,
        should_cancel: Callable[[], bool] | None = None,
        on_collection_progress: Callable[[int], None] | None = None,
    ) -> tuple[list[str], int]:
        self._search_case_types.clear()
        self._search_case_numbers.clear()
        case_ids: list[str] = []
        seen: set[str] = set()

        # Phase 2.2: Parallelize collection. First page for count.
        data_result = await self._request_json_async(
            "GET", self._search_path, params=self._build_query(params, 1)
        )
        data = self._validate_success(data_result.data)
        pages_count = int(data.get("PagesCount", 0) or 0)

        def _process_page(p_data: dict):
            for case in p_data.get("Cases", []):
                cid = case.get("CaseId")
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                case_ids.append(cid)
                c_type = str(case.get("CaseType", "") or "").strip()
                if c_type:
                    self._search_case_types[cid] = c_type
                c_num = str(case.get("CaseNumber", "") or "").strip()
                if c_num:
                    self._search_case_numbers[cid] = c_num
                if on_collection_progress:
                    on_collection_progress(len(case_ids))

        _process_page(data)
        if len(case_ids) >= settings.max_cases or pages_count <= 1:
            return case_ids, pages_count

        max_page = min(settings.max_pages, pages_count)
        page_tasks = [
            self._request_json_async(
                "GET", self._search_path, params=self._build_query(params, p)
            )
            for p in range(2, max_page + 1)
        ]

        results = await asyncio.gather(*page_tasks, return_exceptions=True)
        fail_count = 0
        for res in results:
            if isinstance(res, (BaseException, type(None))):
                fail_count += 1
                continue
            try:
                p_data = self._validate_success(res.data)
                _process_page(p_data)
                if len(case_ids) >= settings.max_cases:
                    break
            except Exception:
                fail_count += 1
                continue

        if pages_count > 1 and fail_count / (max_page - 1) > 0.3:
            raise KadUnavailableError(
                f"Too many pages failed to load ({fail_count}/{max_page - 1}). Data may be incomplete."
            )

        return case_ids, pages_count

    async def _fetch_case_decision_with_metrics(
        self, case_id: str, settings: Settings
    ) -> DecisionFetchOutcome:
        now = datetime.now()
        if self._details_cache_repository is not None:
            try:
                cached = self._details_cache_repository.get(case_id, now)
            except sqlite3.Error as exc:
                cached = None
                log_event(
                    self._logger,
                    "case_details_cache.read_failed",
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
            if cached is not None:
                return DecisionFetchOutcome(
                    decision=cached, retry_count=0, had_transient_error=False
                )

        data_result = await self._request_json_async(
            "GET",
            self._details_by_id_path,
            params={"key": self._api_key, "CaseId": case_id},
        )
        data = self._validate_success(data_result.data)
        cases = data.get("Cases", [])
        if not cases:
            log_event(self._logger, "kad.details_empty", case_id=case_id)
            return DecisionFetchOutcome(
                decision=None,
                retry_count=data_result.retry_count,
                had_transient_error=data_result.had_transient_error,
            )
        case = cases[0]
        # details_by_id response omits CaseType and CaseNumber — inject from search-phase cache.
        if not case.get("CaseType") and case_id in self._search_case_types:
            case["CaseType"] = self._search_case_types[case_id]
        if not case.get("CaseNumber") and case_id in self._search_case_numbers:
            case["CaseNumber"] = self._search_case_numbers[case_id]
        decision = self._build_decision_from_case(
            case,
            settings.max_documents_per_case,
            fallback_case_id=case_id,
        )
        # Skip per-case LLM enrichment when article classification will run later —
        # classify_and_extract already extracts reasons, so this would be double cost.
        if (
            decision is not None
            and self._llm_reason_extractor is not None
            and not self._current_article
            and (
                not decision.reasons
                or decision.reasons == ("оценка обстоятельств дела",)
                or decision.outcome == CaseOutcome.UNKNOWN
            )
        ):
            (
                llm_reasons,
                llm_outcome,
            ) = await self._llm_reason_extractor.extract_with_outcome(decision)

            confidence = 1.0
            conflicts = []

            if llm_outcome:
                if decision.outcome == CaseOutcome.UNKNOWN:
                    confidence -= 0.05
                    if llm_outcome == "satisfied":
                        decision = replace(decision, outcome=CaseOutcome.SATISFIED)
                    elif llm_outcome == "denied":
                        decision = replace(decision, outcome=CaseOutcome.DENIED)
                else:
                    if decision.outcome.value != llm_outcome:
                        confidence -= 1.0
                        conflicts.append(
                            f"Conflict: Rules engine detected '{decision.outcome.value}' but LLM extracted '{llm_outcome}'"
                        )

            decision = replace(
                decision,
                reasons=llm_reasons,
                reason_confidence=0.7,
                confidence_score=max(0.0, confidence),
                validation_conflicts=tuple(conflicts),
            )
        if decision is not None and self._details_cache_repository is not None:
            try:
                self._details_cache_repository.set(
                    case_id,
                    decision,
                    now,
                    ttl_seconds=settings.details_cache_ttl_seconds,
                )
            except sqlite3.Error as exc:
                log_event(
                    self._logger,
                    "case_details_cache.write_failed",
                    case_id=case_id,
                    error_type=type(exc).__name__,
                    error=str(exc),
                )
        return DecisionFetchOutcome(
            decision=decision,
            retry_count=data_result.retry_count,
            had_transient_error=data_result.had_transient_error,
        )

    async def _enrich_unknown_outcomes_with_llm(
        self,
        decisions: list[CaseDecision],
        should_cancel: Callable[[], bool] | None = None,
    ) -> list[CaseDecision]:
        if self._llm_reason_extractor is None or not decisions:
            return decisions

        unknown_indexes = [
            idx for idx, d in enumerate(decisions) if d.outcome == CaseOutcome.UNKNOWN
        ]
        if not unknown_indexes:
            return decisions

        target_indexes = unknown_indexes[: self._MAX_UNKNOWN_OUTCOME_ENRICHMENT]
        skipped = max(0, len(unknown_indexes) - len(target_indexes))
        if skipped > 0:
            log_event(
                self._logger,
                "fetch_decisions.unknown_outcome_enrichment_capped",
                total_unknown=len(unknown_indexes),
                enriched=len(target_indexes),
                skipped=skipped,
            )

        async def _enrich(index: int):
            if should_cancel and should_cancel():
                return index, None
            d = decisions[index]
            reasons, llm_outcome = await self._llm_reason_extractor.extract_with_outcome(d)
            if llm_outcome == "satisfied":
                updated = replace(d, outcome=CaseOutcome.SATISFIED)
            elif llm_outcome == "denied":
                updated = replace(d, outcome=CaseOutcome.DENIED)
            else:
                updated = d

            if reasons and reasons != ("оценка обстоятельств дела",):
                updated = replace(updated, reasons=reasons, reason_confidence=max(updated.reason_confidence, 0.75))
            return index, updated

        tasks = [asyncio.create_task(_enrich(i)) for i in target_indexes]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        enriched = list(decisions)
        updated_outcomes = 0
        for res in results:
            if isinstance(res, BaseException) or res is None:
                continue
            idx, updated = res
            if updated is None:
                continue
            if enriched[idx].outcome == CaseOutcome.UNKNOWN and updated.outcome != CaseOutcome.UNKNOWN:
                updated_outcomes += 1
            enriched[idx] = updated

        if updated_outcomes > 0:
            log_event(
                self._logger,
                "fetch_decisions.unknown_outcome_enrichment_done",
                updated_outcomes=updated_outcomes,
                total_unknown=len(unknown_indexes),
            )

        return enriched

    def _sanitize_params(self, params: SearchParams) -> SearchParams:
        if params.court is not None:
            cached = self._court_validity_cache.get(params.court)
            if cached is False:
                params.use_court_filter = False
            elif cached is True and not params.case_type and not params.inn_or_name:
                return params
        removed: list[str] = []
        while True:
            try:
                data = self._request_json(
                    "GET", self._search_path, params=self._build_query(params, page=1)
                )
                if data.get("Success") == 0:
                    return params
                if removed:
                    log_event(
                        self._logger, "search.filters_removed", removed_filters=removed
                    )
                # Only cache court as definitively valid on a confirmed Success=1 response.
                if params.court is not None and params.use_court_filter:
                    self._court_validity_cache[params.court] = True
                return params
            except KadInvalidResponseError as exc:
                removed_any = self._drop_invalid_filter(params, str(exc), removed)
                if not removed_any:
                    raise
                if "Court" in removed and params.court and not params.use_court_filter:
                    self._court_validity_cache[params.court] = False

    async def _sanitize_params_async(self, params: SearchParams) -> SearchParams:
        if params.court is not None:
            cached = self._court_validity_cache.get(params.court)
            if cached is False:
                params.use_court_filter = False
            elif cached is True and not params.case_type and not params.inn_or_name:
                return params
        removed: list[str] = []
        while True:
            try:
                # Use asdict for logging
                from dataclasses import asdict

                log_debug(self._logger, "search.sanitizing", params=asdict(params))

                result = await self._request_json_async(
                    "GET", self._search_path, params=self._build_query(params, page=1)
                )
                if result.data.get("Success") == 0:
                    return params
                if removed:
                    log_event(
                        self._logger, "search.filters_removed", removed_filters=removed
                    )
                if params.court is not None and params.use_court_filter:
                    self._court_validity_cache[params.court] = True
                return params
            except KadInvalidResponseError as exc:
                removed_any = self._drop_invalid_filter(params, str(exc), removed)
                if not removed_any:
                    raise
                if "Court" in removed and params.court and not params.use_court_filter:
                    self._court_validity_cache[params.court] = False

    def _drop_invalid_filter(
        self, params: SearchParams, error_text: str, removed: list[str]
    ) -> bool:
        normalized = error_text.lower()
        log_event(self._logger, "search.api_error", error=error_text)
        removed_any = False
        if (
            ("court" in normalized or "суд" in normalized)
            and params.court
            and params.use_court_filter
        ):
            params.use_court_filter = False
            removed.append("Court")
            removed_any = True
        if ("case type" in normalized or "тип дела" in normalized) and params.case_type:
            params.case_type = None
            removed.append("CaseType")
            removed_any = True
        if ("inn" in normalized or "инн" in normalized) or ("part" in normalized):
            if params.inn_or_name or params.article:
                params.inn_or_name = None
                params.inn_type = None
                # If the error was about the Inn field, and we put the article there,
                # we must clear it for the search but keep it in params for later local filtering.
                # However, SearchParams is reused, so we have to be careful.
                # The safest thing is to only clear things that go into the query.
                removed.append("Inn/Article")
                removed_any = True
        return removed_any

    def _search(self, params: SearchParams, page: int) -> dict:
        query = self._build_query(params, page)
        data = self._request_json("GET", self._search_path, params=query)
        return self._validate_success(data)

    def _build_query(self, params: SearchParams, page: int) -> dict:
        query = {
            "key": self._api_key,
            "page": page,
        }
        if params.inn_or_name:
            query["Inn"] = params.inn_or_name

        # Priority 1: full_article (e.g. "ст. 723 ГК РФ") — best for full-text precision
        # Priority 2: article (e.g. "723") — fallback
        search_text = params.full_article or params.article
        if search_text:
            query["Text"] = search_text

        if params.inn_type:
            query["InnType"] = params.inn_type
        if params.date_from:
            query["DateFrom"] = params.date_from
        if params.date_to:
            query["DateTo"] = params.date_to
        if params.court and params.use_court_filter:
            query["Court"] = params.court
        if params.case_type:
            query["CaseType"] = params.case_type
        return query

    def _fetch_case_by_number(
        self, case_number: str, max_documents_per_case: int
    ) -> CaseDecision | None:
        data = self._request_json(
            "GET",
            self._details_by_number_path,
            params={"key": self._api_key, "CaseNumber": case_number},
        )
        data = self._validate_success(data)
        cases = data.get("Cases", [])
        if not cases:
            return None
        case = cases[0]
        return self._build_decision_from_case(
            case, max_documents_per_case=max_documents_per_case
        )

    async def _fetch_case_by_number_async(
        self, case_number: str, max_documents_per_case: int
    ) -> CaseDecision | None:
        data_result = await self._request_json_async(
            "GET",
            self._details_by_number_path,
            params={"key": self._api_key, "CaseNumber": case_number},
        )
        data = self._validate_success(data_result.data)
        cases = data.get("Cases", [])
        if not cases:
            return None
        case = cases[0]
        decision = self._build_decision_from_case(
            case, max_documents_per_case=max_documents_per_case
        )
        if (
            decision is not None
            and self._llm_reason_extractor is not None
            and (
                not decision.reasons
                or decision.reasons == ("оценка обстоятельств дела",)
            )
        ):
            llm_reasons = await self._llm_reason_extractor.extract(
                decision.analysis_text, decision.outcome
            )
            decision = replace(decision, reasons=llm_reasons)
        return decision

    def _build_decision_from_case(
        self,
        case: dict,
        max_documents_per_case: int,
        fallback_case_id: str = "",
    ) -> CaseDecision | None:
        case_number = self._extract_case_number(case)
        case_id = self._extract_case_id(case) or fallback_case_id
        court_name = self._extract_court_name(case)
        instances = case.get("CaseInstances", [])
        if not instances:
            return None

        events: list[dict] = []
        for instance in instances:
            events.extend(instance.get("InstanceEvents", []) or [])

        events = sorted(events, key=lambda event: event.get("Date", ""))
        if max_documents_per_case:
            events = events[-max_documents_per_case:]

        outcome, reasons, decision_date, analysis_text, document_links, reason_conf = (
            self._extract_outcome_and_reasons(events)
        )
        case_category = self._extract_case_category(case)
        return CaseDecision(
            case_number=case_number,
            decision_date=decision_date,
            outcome=outcome,
            reasons=reasons,
            case_id=case_id,
            court_name=court_name,
            case_link=self._build_case_link(case_id=case_id, case_number=case_number),
            analysis_text=analysis_text,
            case_category=case_category,
            document_links=document_links,
            reason_confidence=reason_conf,
        )

    def _extract_case_number(self, case: dict) -> str:
        for key in ("CaseNumber", "case_number", "Number", "CaseNo", "caseNo"):
            value = case.get(key)
            if value:
                return str(value).strip()
        return ""

    def _extract_case_id(self, case: dict) -> str:
        for key in ("CaseId", "case_id", "Id", "id"):
            value = case.get(key)
            if value:
                return str(value).strip()
        return ""

    def _extract_court_name(self, case: dict) -> str:
        for key in (
            "CourtName",
            "Court",
            "court_name",
            "court",
            "CourtFullName",
            "CaseCourtName",
        ):
            value = case.get(key)
            if value:
                return self._normalize_court_value(value)
        for instance in case.get("CaseInstances", []) or []:
            for key in ("CourtName", "Court", "court_name", "court"):
                value = instance.get(key)
                if value:
                    return self._normalize_court_value(value)
        return "Суд не указан"

    def _extract_case_category(self, case: dict) -> str:
        """Extract case category (Б/Г/А) directly from the API response dict.

        Tries multiple field names because parser-API wrappers differ.
        Logs all top-level keys once so we can confirm the right field in production.
        """
        if not hasattr(self, "_category_keys_logged"):
            self._category_keys_logged = True
            log_event(self._logger, "case.raw_keys", keys=sorted(case.keys()))

        # Try direct category letter values
        for field in ("CaseType", "Category", "CaseCategory"):
            val = str(case.get(field, "") or "").strip()
            if val in ("Б", "Г", "А"):
                return val
            if val in ("B", "G", "A"):
                return {"B": "Б", "G": "Г", "A": "А"}[val]

        # Try Russian category name strings
        for field in ("CategoryName", "CaseTypeName", "TypeName"):
            val = str(case.get(field, "") or "").lower()
            if not val:
                continue
            if "банкрот" in val or "несостоятельн" in val:
                return "Б"
            if "административ" in val:
                return "А"
            return "Г"  # any other named category → civil

        return ""  # unknown — will fall back to text analysis in _matches_query_scope

    def _normalize_court_value(self, value: object) -> str:
        if isinstance(value, dict):
            for key in ("Name", "name", "FullName", "fullName"):
                nested = value.get(key)
                if nested:
                    return str(nested).strip()
            return "Суд не указан"
        return str(value).strip()

    def _build_case_link(self, case_id: str, case_number: str) -> str:
        if case_id:
            return f"https://kad.arbitr.ru/Card/{case_id}"
        if case_number:
            return f"https://kad.arbitr.ru/?caseNumber={quote_plus(case_number)}"
        return "https://kad.arbitr.ru/"

    def _court_tokens(self, court_name: str | None) -> set[str]:
        if not court_name:
            return set()
        normalized = " ".join(court_name.upper().replace("Ё", "Е").split())
        aas_match = re.search(
            r"\b(\d{1,2})\s*(?:ААС|АРБИТРАЖНЫЙ АПЕЛЛЯЦИОННЫЙ СУД)\b", normalized
        )
        if aas_match:
            return {f"AAS_{aas_match.group(1)}"}
        if "САНКТ" in normalized or "ПЕТЕРБУРГ" in normalized or "СПБ" in normalized:
            return {"САНКТПЕТЕРБУРГ"}
        if "МОСКВ" in normalized:
            return {"МОСКВ"}

        stop_words = {
            "АС",
            "АРБИТРАЖНЫЙ",
            "СУД",
            "ГОРОДА",
            "ГОРОД",
            "ОБЛАСТИ",
            "ОБЛАСТЬ",
            "КРАЯ",
            "КРАЙ",
            "РЕСПУБЛИКИ",
            "РЕСПУБЛИКА",
            "АВТОНОМНОГО",
            "АВТОНОМНЫЙ",
            "ОКРУГА",
            "ОКРУГ",
            "И",
            "ИМЕНИ",
            "ФЕДЕРАЛЬНЫЙ",
            "ФЕДЕРАЛЬНОГО",
        }
        tokens = re.findall(r"[A-ZА-Я0-9]+", normalized)
        result: set[str] = set()
        for token in tokens:
            if token in stop_words:
                continue
            normalized_token = self._normalize_court_token(token)
            if normalized_token:
                result.add(normalized_token)
        return result

    def _normalize_court_token(self, token: str) -> str:
        if token in {"СПБ", "СПБГ", "СПБГУ"}:
            return "САНКТПЕТЕРБУРГ"
        if token in {"ЛО"}:
            return "ЛЕНИНГРАД"
        if token in {"МСК"}:
            return "МОСКВ"
        if token.startswith("САНКТ"):
            return "САНКТПЕТЕРБУРГ"
        if token.startswith("ПЕТЕРБУРГ"):
            return "САНКТПЕТЕРБУРГ"
        if token.startswith("МОСКВ"):
            return "МОСКВ"
        if len(token) > 10:
            return token[:10]
        if len(token) > 7:
            return token[:7]
        return token

    def _courts_match(self, expected_tokens: set[str], actual_tokens: set[str]) -> bool:
        if not expected_tokens or not actual_tokens:
            return False
        if expected_tokens == actual_tokens:
            return True
        overlap = expected_tokens & actual_tokens
        if not overlap:
            return False
        min_size = min(len(expected_tokens), len(actual_tokens))
        return (len(overlap) / min_size) >= 0.6

    def _extract_outcome_and_reasons(
        self, events: list[dict]
    ) -> tuple[CaseOutcome, tuple[str, ...], date, str, tuple[dict[str, str], ...], float]:
        if not events:
            return (
                CaseOutcome.UNKNOWN,
                ("оценка обстоятельств дела",),
                date.today(),
                "",
                (),
                0.1,
            )

        # Each entry: (full_text, priority_text, date, docs)
        # priority_text = only the most structured/reliable fields from the API
        prepared: list[tuple[str, str, date, list[dict[str, str]]]] = []
        for event in events:
            date_str = event.get("Date")
            decision_date = self._parse_date(date_str) if date_str else date.today()
            full_text = self._build_event_text(event)
            # DecisionTypeName is the most reliable structured field (e.g. "Отказать в удовлетворении")
            # ActTypeName and Resolution are also structured decision fields
            priority_text = " ".join(
                filter(
                    None,
                    [
                        str(event.get("DecisionTypeName", "") or ""),
                        str(event.get("ActTypeName", "") or ""),
                        str(event.get("Resolution", "") or ""),
                    ],
                )
            ).strip()

            docs: list[dict[str, str]] = []
            if event.get("FileName") and event.get("Url"):
                docs.append({"name": str(event["FileName"]), "url": str(event["Url"])})
            elif event.get("Details") and isinstance(event["Details"], list):
                for detail in event["Details"]:
                    if detail.get("FileName") and detail.get("Url"):
                        docs.append({
                            "name": str(detail["FileName"]),
                            "url": str(detail["Url"]),
                        })

            prepared.append((full_text, priority_text, decision_date, docs))

        decisive_idx = len(prepared) - 1
        outcome = CaseOutcome.UNKNOWN

        # Pass 1: use only structured priority fields — highest accuracy
        for idx in range(len(prepared) - 1, -1, -1):
            priority_text = prepared[idx][1]
            if priority_text:
                mapped = self._map_outcome(priority_text)
                if mapped != CaseOutcome.UNKNOWN:
                    decisive_idx = idx
                    outcome = mapped
                    break

        # Pass 2: fall back to full concatenated event text
        if outcome == CaseOutcome.UNKNOWN:
            for idx in range(len(prepared) - 1, -1, -1):
                mapped = self._map_outcome(prepared[idx][0])
                if mapped != CaseOutcome.UNKNOWN:
                    decisive_idx = idx
                    outcome = mapped
                    break

        decision_date = prepared[decisive_idx][2]

        # Concatenate ALL event texts for better recall (Phase 3)
        # We add markers so the LLM understands it's looking at a history of events.
        concatenated_parts = []
        for i, (full_text, _, _, _) in enumerate(prepared):
            concatenated_parts.append(f"--- СОБЫТИЕ №{i + 1} ---\n{full_text}")
        analysis_text = "\n\n".join(concatenated_parts)

        all_docs: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        for _, _, _, docs in reversed(prepared):
            for d in docs:
                if d["url"] not in seen_urls:
                    all_docs.append(d)
                    seen_urls.add(d["url"])

        # Reasons extraction still focuses on the decisive text, but fallback uses the combined text
        reasons, reason_conf = self._reason_extractor.extract_with_confidence(prepared[decisive_idx][0])
        if not reasons or reasons == ("оценка обстоятельств дела",):
            reasons, reason_conf = self._reason_extractor.extract_with_confidence(analysis_text)

        if not reasons:
            reasons = ("оценка обстоятельств дела",)
            reason_conf = 0.1

        return outcome, reasons, decision_date, analysis_text, tuple(all_docs), reason_conf

    def _build_event_text(self, event: dict) -> str:
        return " ".join([
            str(event.get("EventTypeName", "")),
            str(event.get("DecisionTypeName", "")),
            str(event.get("EventContentTypeName", "")),
            str(event.get("AdditionalInfo", "")),
            str(event.get("Comment", "")),
            str(event.get("DocumentTypeName", "")),
            str(event.get("ActTypeName", "")),
            str(event.get("Text", "")),
            str(event.get("Description", "")),
            str(event.get("Resolution", "")),
        ])

    def _matches_query_scope(
        self, decision: CaseDecision, params: SearchParams
    ) -> bool:
        text = (decision.analysis_text or "").lower()

        # Unconditional exclusions — applied regardless of query params
        if self._is_returned_application(text):
            return False
        if (
            self._is_operative_part_only(text)
            and decision.outcome == CaseOutcome.UNKNOWN
        ):
            return False

        # No filtering criteria at all → accept
        if not params.article and not params.paragraph and not params.case_type:
            return True

        # Article-only filter is intentionally omitted.
        # KAD full-text search already matched the article in case documents.
        # Event titles (what analysis_text is built from) are short court-event summaries
        # that never mention article numbers, so re-checking them drops ~60% of valid cases.

        # Paragraph filter — applied only when a specific paragraph is requested.
        # Paragraph-level queries (e.g. п.2 ст.61.3) differ meaningfully between paragraphs,
        # and KAD search may return mixed paragraph results.
        if params.paragraph:
            full_text = " ".join(
                filter(
                    None,
                    [
                        decision.analysis_text or "",
                        " ".join(decision.reasons or []),
                    ],
                )
            ).lower()
            if not full_text:
                return False
            if params.article and not self._text_has_article(full_text, params.article):
                return False
            if not self._text_has_paragraph(
                full_text, params.paragraph, params.article
            ):
                return False

        # Case type filtering — skipped when an article is specified.
        # Article-specific queries (e.g. ст.61.2) target adversarial proceedings
        # that KAD classifies as Г (civil), not Б (bankruptcy main case).
        # Filtering by CaseType here would drop exactly the cases the user wants;
        # the article text filter above is more reliable for these queries.
        if params.case_type and not params.article:
            if decision.case_category:
                # Direct API category — 100% accurate, works for cached cases too
                if decision.case_category != self._CASE_TYPE_TO_CATEGORY.get(
                    params.case_type, ""
                ):
                    return False
            elif text:
                # Text-based fallback — no 100-char minimum (was causing bypasses)
                if not self._text_matches_case_type(text, params.case_type):
                    return False

        return True

    def _text_has_article(self, text: str, article: str) -> bool:
        normalized_article = article.replace(",", ".")
        compact = normalized_article.replace(".", "\\.")
        patterns = [
            rf"ст\.?\s*{compact}\b",
            rf"стать[ьяи]\s*{compact}\b",
            rf"\b{compact}\b",
        ]
        return any(re.search(pattern, text) for pattern in patterns)

    def _text_has_paragraph(
        self, text: str, paragraph: str, article: str | None
    ) -> bool:
        paragraph_patterns = [
            rf"п\.?\s*{re.escape(paragraph)}\b",
            rf"пункт\s*{re.escape(paragraph)}\b",
        ]
        if article:
            normalized_article = article.replace(",", ".").replace(".", "\\.")
            combined = [
                rf"(п\.?\s*{re.escape(paragraph)}|пункт\s*{re.escape(paragraph)}).{{0,32}}(ст\.?\s*{normalized_article}|стать[ьяи]\s*{normalized_article})",
                rf"(ст\.?\s*{normalized_article}|стать[ьяи]\s*{normalized_article}).{{0,32}}(п\.?\s*{re.escape(paragraph)}|пункт\s*{re.escape(paragraph)})",
            ]
            if any(re.search(pattern, text) for pattern in combined):
                return True
        return any(re.search(pattern, text) for pattern in paragraph_patterns)

    def _is_returned_application(self, text: str) -> bool:
        return (
            "заявление возвращен" in text
            or "возврат заявлени" in text
            or "возвращено заявлени" in text
        )

    def _is_operative_part_only(self, text: str) -> bool:
        """True when decisive document is only the operative part (no reasoning published)."""
        if len(text) < 50 or "резолютивная" not in text:
            return False
        reasoning_markers = (
            "установил",
            "рассмотрев",
            "материалы дела",
            "обстоятельства",
        )
        return not any(m in text for m in reasoning_markers)

    def _text_matches_case_type(self, text: str, case_type: str) -> bool:
        if case_type == "B":
            return any(m in text for m in self._BANKRUPTCY_MARKERS)
        if case_type == "G":
            return not any(m in text for m in self._BANKRUPTCY_MARKERS) and not any(
                m in text for m in self._ADMIN_MARKERS
            )
        if case_type == "A":
            return any(m in text for m in self._ADMIN_MARKERS)
        return True  # unknown case type — don't filter

    _CASE_TYPE_TO_CATEGORY: dict[str, str] = {"B": "Б", "G": "Г", "A": "А"}

    _BANKRUPTCY_MARKERS: frozenset[str] = frozenset({
        "банкрот",
        "несостоятельн",
        "конкурсн",
        "наблюден",
        "финансовое оздоровлен",
        "арбитражный управляющ",
        "конкурсный управляющ",
        "временный управляющ",
    })
    _ADMIN_MARKERS: frozenset[str] = frozenset({
        "коап",
        "административное правонарушен",
        "административн ответственност",
    })

    _DENIED_COMBINED = [
        re.compile(
            r"отказ\w*(?:\s+\w+){0,3}\s+в\s+(?:удовлетвор|признани|иске|заявлении|жалобе|требован|привлечении)\w*"
        ),
        re.compile(
            r"(?:удовлетвор|признани|иске|заявлении|жалобе|требован|привлечении)\w*(?:\s+\w+){0,10}\s+отказ\w*"
        ),
        re.compile(r"без\s+(?:удовлетвор|рассмотрения)\w*"),
        re.compile(r"производство\w*\s+(?:.+?\s+)?прекратить"),
        re.compile(r"прекратить\s+производство"),
        re.compile(r"необоснованн\w*"),
        re.compile(r"основани\w*\s+(?:.+?\s+)?отсутству\w*"),
        re.compile(r"отсутству\w+\s+основани\w+"),
        re.compile(r"не\s+подлежит\s+(?:удовлетвор|признани)\w*"),
        re.compile(r"не\s+(?:может|мог|могла|могло)\s+быть\s+признан\w*"),
        re.compile(r"не\s+(?:было\s+)?установлено\w*"),
        re.compile(r"не\s+усматривается"),
        re.compile(r"признак\w*(?:\s+\w+){0,5}\s+(?:не\s+установлен|отсутству)"),
        # Common appeal/complaint denial patterns
        re.compile(r"оставить\s+(?:\w+\s+){0,3}без\s+(?:удовлетвор|изменен)\w*"),
        re.compile(r"оставлен\w*\s+без\s+(?:удовлетвор|изменен)\w*"),
        re.compile(r"жалоб\w+\s+(?:\w+\s+){0,5}не\s+(?:обоснован|подлежит)\w*"),
        re.compile(r"не\s+(?:нашел|находит|усматрива\w+)\s+основани\w*"),
        re.compile(r"не\s+(?:нашел|находит)\s+(?:\w+\s+){0,3}основани\w*"),
        re.compile(r"заявлени\w+\s+(?:\w+\s+){0,5}не\s+подлежит\s+удовлетвор\w*"),
    ]

    # Keywords that — when present WITHOUT a preceding denial context — indicate SATISFIED.
    _SATISFIED_KEYWORDS = [
        "удовлетвор",  # удовлетворить / удовлетворено / удовлетворены
        "признать недействит",
        "признать незаконн",
        "признано незаконн",
        "незаконн",
        "неправомерн",
        "ненадлежащ",  # ненадлежащее исполнение (признать)
        "взыскать",
        "отстранить",  # отстранить арбитражного управляющего
        "привлечь к",  # привлечь к ответственности
        "обоснованн",  # жалоба признана обоснованной
        "отменить определени",  # отменить определение (reverse ruling)
        "изменить определени",  # изменить определение (modify ruling)
        "жалоба подлежит удовлетвор",  # complaint subject to satisfaction
        "заявление подлежит удовлетвор",  # application subject to satisfaction
        "признать действия",  # признать действия незаконными (already covered by незаконн)
        "снизить",  # снизить размер вознаграждения
        "уменьшить",  # уменьшить размер
        "включить в реестр",  # включить требования в реестр кредиторов
    ]

    async def _refine_params_with_llm(
        self, query_text: str, params: SearchParams
    ) -> SearchParams:
        if self._llm_reason_extractor is None:
            return params

        llm_params = await self._llm_reason_extractor.parse_query(query_text)

        # Extract period-related fields
        year = llm_params.get("year") or (
            params.date_from[:4] if params.date_from else None
        )

        # Guardrail: If regex already found a quarter, and LLM output differs or is null,
        # we trust the regex more (it's "cheap" and deterministic).
        llm_quarter = llm_params.get("quarter")
        if params._regex_quarter and (
            not llm_quarter or str(llm_quarter) != str(params._regex_quarter)
        ):
            log_event(
                self._logger,
                "kad.llm_quarter_ignored",
                regex=params._regex_quarter,
                llm=llm_quarter,
            )
            final_quarter = params._regex_quarter
        else:
            final_quarter = llm_quarter or params._regex_quarter

        llm_date_from = llm_params.get("date_from")
        llm_date_to = llm_params.get("date_to")

        if llm_date_from or llm_date_to:
            date_from = llm_date_from or params.date_from
            date_to = llm_date_to or params.date_to
        else:
            date_from, date_to = self._parser._build_period(year, final_quarter)

        llm_court = llm_params.get("court")
        if llm_court:
            # Try to normalize LLM output using our regex-based rules
            normalized_llm_court = self._parser._extract_court(llm_court)
            final_court = normalized_llm_court or llm_court
        else:
            final_court = params.court

        # Normalize case types to KAD API format (Latin: G/B/A).
        type_map = {"G": "G", "B": "B", "A": "A", "Г": "G", "Б": "B", "А": "A"}
        llm_type = llm_params.get("case_type")

        # Defensive: if the parser already identified Bankruptcy (B) for Article 61.2,
        # don't let the LLM downgrade it to General (G).
        if params.case_type == "B":
            final_type = "B"
        else:
            final_type = type_map.get(llm_type, llm_type) or params.case_type

        # Merge with regex results, preferring LLM for these fields
        refined = replace(
            params,
            article=llm_params.get("article") or params.article,
            full_article=llm_params.get("full_article") or params.full_article,
            court=final_court,
            case_type=final_type,
            date_from=date_from or params.date_from,
            date_to=date_to or params.date_to,
        )

        # Use asdict for logging to avoid JSON serialization errors
        from dataclasses import asdict

        log_event(self._logger, "kad.llm_query_parsed", params=asdict(refined))
        return refined

    def _map_outcome(self, text: str) -> CaseOutcome:
        lower = text.lower()

        # Step 1: combined DENIED patterns take highest priority
        for pattern in self._DENIED_COMBINED:
            if pattern.search(lower):
                return CaseOutcome.DENIED

        # Step 2: satisfied keywords (safe now — combined DENIED already excluded)
        for keyword in self._SATISFIED_KEYWORDS:
            if keyword in lower:
                return CaseOutcome.SATISFIED

        # Step 3: standalone denial
        if "отказ" in lower:
            return CaseOutcome.DENIED
        if "прекратить производство" in lower:
            return CaseOutcome.DENIED

        return CaseOutcome.UNKNOWN

    def _request_json(self, method: str, path: str, params: dict) -> dict:
        url = f"{self._base_url}{path}"
        rate_limit_attempts = 0
        unavailable_attempts = 0
        last_error: Exception | None = None
        for _ in range(5):
            try:
                response = self._sync_http_client.request(
                    method, url, params=params, timeout=self._timeout
                )
                if response.status_code == 429:
                    rate_limit_attempts += 1
                    if rate_limit_attempts >= 3:
                        raise KadRateLimitError("KAD API rate limit exceeded")
                    log_event(
                        self._logger, "kad.rate_limit", attempt=rate_limit_attempts
                    )
                    self._sleep(60)
                    continue
                if response.status_code in {503, 504}:
                    unavailable_attempts += 1
                    if unavailable_attempts >= 2:
                        raise KadUnavailableError("KAD API unavailable")
                    log_event(
                        self._logger, "kad.unavailable", attempt=unavailable_attempts
                    )
                    continue
                if response.status_code == 500:
                    unavailable_attempts += 1
                    if unavailable_attempts >= 2:
                        raise KadUnavailableError("KAD API error 500")
                    log_event(
                        self._logger, "kad.server_error", attempt=unavailable_attempts
                    )
                    continue
                if response.status_code == 403:
                    data = response.json()
                    raise KadAccessError(
                        str(data.get("error", "KAD API access denied"))
                    )
                if response.status_code == 400:
                    data = response.json()
                    raise KadInvalidResponseError(
                        str(data.get("error", "Invalid request"))
                    )
                response.raise_for_status()
                data = response.json()
                if data.get("Success") == 0 and "error" not in data:
                    # Fast Success=0 means "0 results" or unrecognized params —
                    # not a transient error worth retrying.
                    log_debug(self._logger, "kad.success_zero")
                    return data
                return data
            except httpx.TimeoutException as exc:
                unavailable_attempts += 1
                last_error = exc
                if unavailable_attempts >= 2:
                    raise KadUnavailableError("KAD API timeout") from exc
            except httpx.HTTPError as exc:
                last_error = exc
        raise KadInvalidResponseError("KAD API request failed") from last_error

    async def _request_json_async(
        self, method: str, path: str, params: dict
    ) -> RequestResult:
        url = f"{self._base_url}{path}"
        rate_limit_attempts = 0
        unavailable_attempts = 0
        retry_count = 0
        had_transient_error = False
        last_error: Exception | None = None
        for _ in range(5):
            try:
                response = await self._async_http_client.request(
                    method, url, params=params, timeout=self._timeout
                )
                if response.status_code == 429:
                    retry_count += 1
                    had_transient_error = True
                    rate_limit_attempts += 1
                    if rate_limit_attempts >= 3:
                        raise KadRateLimitError("KAD API rate limit exceeded")
                    log_event(
                        self._logger, "kad.rate_limit", attempt=rate_limit_attempts
                    )
                    await asyncio.sleep(60)
                    continue
                if response.status_code in {503, 504}:
                    retry_count += 1
                    had_transient_error = True
                    unavailable_attempts += 1
                    if unavailable_attempts >= 2:
                        raise KadUnavailableError("KAD API unavailable")
                    log_event(
                        self._logger, "kad.unavailable", attempt=unavailable_attempts
                    )
                    continue
                if response.status_code == 500:
                    retry_count += 1
                    had_transient_error = True
                    unavailable_attempts += 1
                    if unavailable_attempts >= 2:
                        raise KadUnavailableError("KAD API error 500")
                    log_event(
                        self._logger, "kad.server_error", attempt=unavailable_attempts
                    )
                    continue
                if response.status_code == 403:
                    data = response.json()
                    raise KadAccessError(
                        str(data.get("error", "KAD API access denied"))
                    )
                if response.status_code == 400:
                    data = response.json()
                    raise KadInvalidResponseError(
                        str(data.get("error", "Invalid request"))
                    )
                response.raise_for_status()
                data = response.json()
                if data.get("Success") == 0 and "error" not in data:
                    if path == self._details_by_id_path:
                        # Success=0 for an ID we just found in /search is a silent overload/rate limit
                        retry_count += 1
                        had_transient_error = True
                        unavailable_attempts += 1
                        log_event(
                            self._logger,
                            "kad.silent_drop_retry",
                            path=path,
                            attempt=unavailable_attempts,
                        )
                        if unavailable_attempts < 2:
                            await asyncio.sleep(2)
                            continue
                        # If we've retried and it still drops, raise unavailable instead of returning empty
                        raise KadUnavailableError(
                            "KAD API silently dropping case details"
                        )

                    log_debug(self._logger, "kad.success_zero")
                return RequestResult(
                    data=data,
                    retry_count=retry_count,
                    had_transient_error=had_transient_error,
                )
            except httpx.TimeoutException as exc:
                retry_count += 1
                had_transient_error = True
                unavailable_attempts += 1
                last_error = exc
                if unavailable_attempts >= 2:
                    raise KadUnavailableError("KAD API timeout") from exc
            except httpx.HTTPError as exc:
                last_error = exc
        raise KadInvalidResponseError("KAD API request failed") from last_error

    def _validate_success(self, data: dict) -> dict:
        if "error" in data:
            raise KadAccessError(str(data.get("error")))
        return data

    def _sleep(self, seconds: int) -> None:
        from time import sleep

        sleep(seconds)

    def _parse_date(self, value: str) -> date:
        try:
            return datetime.fromisoformat(value).date()
        except ValueError:
            return date.today()
