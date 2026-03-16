from __future__ import annotations

import asyncio
import re
import time
import logging
from typing import Callable, Any, Protocol
from dataclasses import replace, asdict
from src.infrastructure.models import Decision
from src.domain.entities import CaseDecision, CaseOutcome
from src.domain.case_models import (
    FetchDecisionsResult,
    FetchStats,
    SearchParams,
)
from src.services.query_parser import QueryParser
from src.domain.settings import Settings
from src.app.bot_logging import log_event


class CaseClient(Protocol):
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


class DatabaseCaseClient:
    def __init__(self, llm_reason_extractor=None) -> None:
        self._parser = QueryParser()
        self._llm_reason_extractor = llm_reason_extractor
        self._logger = logging.getLogger("db_case_client")

    def count_cases(self, query_text: str, settings: Settings) -> int:
        params = self._parser.parse(query_text)
        # We don't necessarily need LLM refinement for counting to keep it fast,
        # but ParserApiKadClient didn't use LLM for counting either.
        query = self._build_query(params)
        return query.count()

    async def count_cases_async(self, query_text: str, settings: Settings) -> int:
        params = self._parser.parse(query_text)
        params = await self._refine_params_with_llm(query_text, params)
        query = self._build_query(params)
        count = query.count()
        log_event(self._logger, "db.count_cases", count=count, params=asdict(params))
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
        
        if on_stage_change:
            on_stage_change("collecting")

        collect_start = time.perf_counter()
        query = self._build_query(params)
        query = query.limit(settings.max_cases)

        results = list(query)
        case_id_collection_ms = int((time.perf_counter() - collect_start) * 1000)

        if on_stage_change:
            on_stage_change("analyzing")

        details_start = time.perf_counter()
        fetched_decisions: list[CaseDecision] = []

        for i, row in enumerate(results):
            if callable(should_cancel) and should_cancel():
                break

            decision = self._map_to_case_decision(row)
            fetched_decisions.append(decision)

            if callable(on_progress):
                on_progress(i + 1)
            # Pipeline will handle 'on_successful' if we return it here, 
            # but DatabaseCaseClient should stick to Stage A responsibilities.

        details_fetch_ms = int((time.perf_counter() - details_start) * 1000)

        stats = FetchStats(
            attempted_cases=len(results),
            successful_cases=len(fetched_decisions),
            retry_count=0,
            effective_concurrency=1,
            case_id_collection_ms=case_id_collection_ms,
            details_fetch_ms=details_fetch_ms,
            filtered_by_court=0,
            court_compared_cases=0,
            total_pages=1,
            total_cases_found=len(results),
        )

        return FetchDecisionsResult(
            decisions=fetched_decisions,
            stats=stats,
            params=params,
        )

    def _build_query(self, params: SearchParams):
        query = Decision.select()

        if params.case_number:
            query = query.where(Decision.case_number.ilike(f"%{params.case_number}%"))

        if params.date_from:
            query = query.where(Decision.date >= params.date_from)
        if params.date_to:
            query = query.where(Decision.date <= params.date_to)

        if params.court:
            # Flexible court/place matching
            court_clean = params.court.replace("АС ", "").replace("города ", "").replace("области", "").strip()
            city_hint = court_clean.split()[0] if court_clean else ""
            if len(city_hint) > 4:
                city_hint = city_hint[:5]
            
            if city_hint:
                # Search in place OR in text to catch city/region mismatches
                query = query.where(
                    (Decision.place.ilike(f"%{city_hint}%")) | 
                    (Decision.text.ilike(f"%Арбитражный суд {city_hint}%")) |
                    (Decision.text.ilike(f"%АС {city_hint}%"))
                )
            else:
                query = query.where(Decision.place.ilike(f"%{params.court}%"))

        if params.article:
            query = query.where(
                Decision.text.ilike(f"%{params.article}%") |
                Decision.article.ilike(f"%{params.article}%")
            )

        return query

    async def _refine_params_with_llm(
        self, query_text: str, params: SearchParams
    ) -> SearchParams:
        if self._llm_reason_extractor is None:
            return params

        llm_params = await self._llm_reason_extractor.parse_query(query_text)
        
        # Merge logic (replicated from kad_client.py for parity)
        year = llm_params.get("year") or (
            params.date_from[:4] if params.date_from else None
        )

        llm_quarter_raw = llm_params.get("quarter")
        llm_quarter: int | None = int(llm_quarter_raw) if llm_quarter_raw is not None and str(llm_quarter_raw).isdigit() else None

        final_quarter: int | None = None
        if params._regex_quarter and (
            not llm_quarter or str(llm_quarter) != str(params._regex_quarter)
        ):
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
        final_court: str | None = None
        if llm_court:
            normalized_llm_court = self._parser._extract_court(str(llm_court))
            final_court = normalized_llm_court or str(llm_court)
        else:
            final_court = params.court

        type_map = {"G": "G", "B": "B", "A": "A", "Г": "G", "Б": "B", "А": "A"}
        llm_type = llm_params.get("case_type")
        mapped_type: str | None = None
        if isinstance(llm_type, str):
            mapped_type = type_map.get(llm_type, llm_type)

        if params.case_type == "B":
            final_type: str | None = "B"
        else:
            final_type = mapped_type or params.case_type

        refined = replace(
            params,
            article=llm_params.get("article") or params.article,
            full_article=llm_params.get("full_article") or params.full_article,
            court=final_court,
            case_type=final_type,
            date_from=date_from or params.date_from,
            date_to=date_to or params.date_to,
        )

        log_event(self._logger, "db.llm_query_parsed", params=asdict(refined))
        return refined

    def _map_to_case_decision(self, row: Decision) -> CaseDecision:
        outcome = self._map_outcome(row.text)
        court_name = self._extract_court_from_text(row.text) or row.place or ""
        
        # Extract act title: "Решение от ..." -> "Решение"
        act_title = ""
        first_line = row.text.split("\n")[0] if row.text else ""
        act_match = re.match(r"^(Решение|Постановление|Определение)", first_line)
        if act_match:
            act_title = act_match.group(1)

        return CaseDecision(
            case_number=row.case_number,
            decision_date=row.date,
            outcome=outcome,
            reasons=("оценка обстоятельств дела",),
            case_id=row.number,
            court_name=court_name,
            case_link=f"https://sudact.ru{row.url}" if row.url else "",
            analysis_text=row.text,
            decisive_act_title=act_title or first_line[:50].strip(),
            decisive_act_type="merits_act" if act_title in ("Решение", "Постановление") else "other",
            raw_number=row.number,
            raw_date=row.date,
            raw_case_number=row.case_number,
            raw_place=row.place or "",
            raw_judge=row.judge or "",
            raw_url=f"https://sudact.ru{row.url}" if row.url else "",
            raw_article=row.article or "",
            raw_text=row.text,
        )

    def _extract_court_from_text(self, text: str) -> str | None:
        # Pattern: Арбитражный суд ... (АС ...)
        match = re.search(r"Арбитражный суд\s+([А-Яа-яЁё\s-]+)\s*(?:\(АС\s+([А-Яа-яЁё\s-]+)\))?", text)
        if match:
            base = match.group(1).strip()
            # If (АС ...) is present and has shorter/better name, use it or combine
            return f"Арбитражный суд {base}"
        return None

    def _map_outcome(self, text: str) -> CaseOutcome:
        lower = text.lower()
        
        # Original DENIED patterns
        denied_patterns = [
            r"отказа\w*\s+(?:в\s+)?удовлетворени\w*",
            r"в\s+удовлетворени\w*(?:\s+\w+){0,8}\s+отказа\w*",
            r"остави\w*(?:\s+\w+)?\s+без\s+удовлетворени\w*",
            r"отказа\w*\s+в\s+иске",
            r"в\s+иске\s+отказа\w*",
            r"жалоб[уа]\s+остави\w*(?:\s+\w+)?\s+без\s+рассмотрения",
            r"остави\w*(?:\s+\w+)?\s+жалобу\s+без\s+удовлетворени\w*",
            r"отказа\w*\s+в\s+признании",
            r"не\s+подлежит\s+признанию",
            r"не\s+может\s+быть\s+признан\w*",
            r"не\s+мог(?:ла)?\s+быть\s+признан\w*",
            r"отсутствуют\s+основания\s+для\s+признания",
            r"признаки\s+.*не\s+установлены",
            r"признаки\s+.*отсутствуют",
            r"в\s+удовлетворении\s+жалобы\s+отказ",
            r"производство\s+по\s+жалобе\s+прекратить",
            r"оставить\s+без\s+рассмотрения",
            r"жалобу\s+признать\s+необоснованной",
            r"правовые\s+основания\s+для\s+удовлетворения\s+жалобы\s+отсутствуют",
            r"отказ\w*(?:\s+\w+){0,3}\s+в\s+(?:удовлетвор|признани|иске|заявлении|жалобе|требован|привлечении)\w*",
            r"без\s+(?:удовлетвор|рассмотрения)\w*",
            r"прекратить\s+производство",
        ]
        
        for p in denied_patterns:
            if re.search(p, lower):
                return CaseOutcome.DENIED

        satisfied_keywords = [
            "удовлетвор",
            "признать недействит",
            "признано незаконным",
            "признать незаконн",
            "взыскать",
            "ненадлежащим исполнение",
            "привлечь к административной ответственности",
            "жалоба признана обоснованной",
        ]
        
        for k in satisfied_keywords:
            if k in lower:
                return CaseOutcome.SATISFIED

        return CaseOutcome.UNKNOWN

    async def aclose(self) -> None:
        pass
