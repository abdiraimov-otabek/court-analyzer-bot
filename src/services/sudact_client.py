from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import replace
from datetime import datetime
from typing import Callable

import httpx
from bs4 import BeautifulSoup

from src.app.bot_logging import log_event
from src.domain.case_models import (
    FetchDecisionsResult,
    FetchStats,
    SearchParams,
    KadUnavailableError,
    KadInvalidResponseError,
)
from src.domain.entities import CaseDecision, CaseOutcome
from src.domain.settings import Settings
from src.domain.outcome_mapper import OutcomeMapper
from src.services.query_parser import QueryParser

logger = logging.getLogger("sudact_client")

# sudact.ru base URL
SUDACT_BASE = "https://sudact.ru"

# Category prefix mappings for date/text query params
# sudact uses category-specific param prefixes like arbitral-date_from, regular-date_from etc.
CATEGORY_PREFIX = "arbitral"  # Arbitration courts — default focus


def _month_to_str(month: int) -> str:
    months = {
        1: "янв", 2: "фев", 3: "мар", 4: "апр", 5: "мая", 6: "июн",
        7: "июл", 8: "авг", 9: "сен", 10: "окт", 11: "ноя", 12: "дек",
    }
    return months.get(month, "???")


# Canonical court name mapping for sudact.ru
COURT_MAPPING = {
    "АС города Москвы": "Арбитражный суд города Москвы",
    "АС Московской области": "Арбитражный суд Московской области",
    "АС города Санкт-Петербурга и Ленинградской области": "Арбитражный суд города Санкт-Петербурга и Ленинградской области",
}


def _normalize_date_str(date_str: str | None) -> str | None:
    """Convert YYYY-MM-DD to DD.MM.YYYY for sudact.ru query params."""
    if not date_str:
        return None
    try:
        parts = date_str.split("-")
        if len(parts) == 3:
            return f"{parts[2]}.{parts[1]}.{parts[0]}"
    except Exception:
        pass
    return None


def _parse_date_from_title(title: str):
    """Extract date from russian title like 'Решение от 10 марта 2023 г. по делу ...'"""
    months = {
        "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5,
        "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    }
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", title.lower())
    if m:
        day, month_str, year = int(m.group(1)), m.group(2)[:3], int(m.group(3))
        month = months.get(month_str)
        if month:
            try:
                from datetime import date
                return date(year, month, day)
            except ValueError:
                pass
    from datetime import date
    return date.today()


def _extract_judge(text: str) -> str | None:
    pattern = re.compile(
        r'[А-Я][а-я]+\s[А-Я]\.(?:\s+|)[А-Я]\.'
        r'|[А-Я]\.(?:\s|)[А-Я]\.\s[А-Я][а-я]+'
    )
    m = pattern.search(text)
    return m.group() if m else None


def _extract_place(text: str) -> str | None:
    m = re.search(r'(?:город|г\.)\s*([А-Я][а-я]+(?:-[А-Яа-я][а-я]+)*(?:\sНовгород)?)', text)
    if m:
        return m.group(1)
    for city in ("Санкт-Петербург", "Красноярск", "Нижний Новгород"):
        if city in text:
            return city
    return None


def _extract_court_from_page(html) -> str | None:
    """Try to read the structured court name from sudact.ru HTML metadata."""
    # sudact.ru usually has a breadcrumb or header with the court name
    for selector in [
        {"class_": "court-name"},
        {"class_": "h-col-name-court"},
        {"id": "court-name"},
    ]:
        tag = html.find(**selector)  # type: ignore[arg-type]
        if tag:
            name = tag.get_text(" ", strip=True)
            if name:
                return name

    # Fallback: look for the court name in breadcrumb links
    crumbs = html.find_all("a", class_="breadcrumb-item")
    for crumb in crumbs:
        text = crumb.get_text(" ", strip=True)
        if any(kw in text for kw in ("суд", "Суд", "СУД")):
            return text

    # Fallback 2: look for known court phrases in <title> or <h1>
    for tag in html.find_all(["title", "h1"]):
        text = tag.get_text(" ", strip=True)
        m = re.search(
            r'(Арбитражный\s+суд[^,\n<]{3,60})', text, re.I
        )
        if m:
            return m.group(1).strip()
    return None


def _extract_article(html_container) -> str | None:
    if not html_container:
        return None
    law_links = html_container.find_all("a", href=re.compile(r"/law/.*statia"))
    if law_links:
        return law_links[0].text.strip()
    text = html_container.get_text("\n")
    m = re.search(
        r'(?i)ст\.\s*\d+(?:\.\d+)?(?:\s+(?:УК|ГК|КоАП|АПК|ГПК|УПК)\s+РФ)?', text
    )
    if m:
        return m.group(0).strip()
    return None


# Local outcome extraction removed, using OutcomeMapper instead.


class SudactClient:
    """
    Live sudact.ru scraper client.

    Satisfies the CaseClient protocol — used as a drop-in replacement
    for DatabaseCaseClient / ParserApiKadClient.

    For each user query it:
      1. Parses the query with QueryParser (date, article, court, etc.)
      2. Builds sudact.ru search URLs for /arbitral/ category
      3. Collects case links from result pages (up to max_cases)
      4. Fetches individual decision pages for full text / outcome
      5. Returns CaseDecision objects wrapped in FetchDecisionsResult
    """

    _CATEGORY_URL = "/arbitral/doc_ajax/"
    _PARAM_PREFIX = "arbitral"

    def __init__(
        self,
        async_http_client: httpx.AsyncClient | None = None,
        llm_reason_extractor=None,
        concurrency: int = 8,
        page_concurrency: int = 5,
    ) -> None:
        self._parser = QueryParser()
        self._llm_reason_extractor = llm_reason_extractor
        self._outcome_mapper = OutcomeMapper()
        self._concurrency = concurrency
        self._page_concurrency = page_concurrency
        self._owns_client = async_http_client is None
        self._client = async_http_client or httpx.AsyncClient(
            timeout=30,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Protocol methods
    # ------------------------------------------------------------------

    def count_cases(self, query_text: str, settings: Settings) -> int:
        """Synchronous count — runs async under the hood via asyncio.run."""
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Already in async context — return estimate based on first page probe
                return 0  # will be resolved async
            return loop.run_until_complete(self.count_cases_async(query_text, settings))
        except Exception:
            return 0

    async def count_cases_async(self, query_text: str, settings: Settings) -> int:
        params = self._parser.parse(query_text)
        url = self._build_page_url(params, page=1)
        try:
            page_html = await self._fetch_page_html(url)
            if not page_html:
                return 0
            total_text = self._extract_total_count_text(page_html)
            if total_text:
                return min(total_text, settings.max_cases + 1)
            items = self._extract_list_items(page_html)
            if not items:
                return 0
            # Rough estimate: 10 items/page × max_pages
            return min(len(items) * settings.max_pages, settings.max_cases + 1)
        except Exception as exc:
            log_event(logger, "sudact.count_error", error=str(exc))
            return 0

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

        # Phase 1: collect decision URLs from search result pages
        case_links: list[tuple[str, str, str]] = []  # (url, title, case_number)
        page = 1
        max_pages = settings.max_pages
        max_cases = settings.max_cases

        sem = asyncio.Semaphore(self._page_concurrency)

        async def fetch_list_page(p: int) -> list[tuple[str, str, str]]:
            url = self._build_page_url(params, page=p)
            async with sem:
                html = await self._fetch_page_html(url)
            if not html:
                return []
            return self._extract_list_items(html)

        # Fetch first page to see if there are results
        first_items = await fetch_list_page(1)
        if not first_items:
            return self._empty_result(params)

        case_links.extend(first_items)
        if on_collection_progress:
            on_collection_progress(len(case_links))

        # Fetch remaining pages in batches until we hit max_cases or max_pages
        page = 2
        empty_streak = 0
        max_empty_retries = 5  # Give it 5 batches (e.g., 15 empty pages) before quitting

        while len(case_links) < max_cases and page <= max_pages:
            if should_cancel and should_cancel():
                break
                
            batch_end = min(page + self._page_concurrency, max_pages + 1)
            tasks = [fetch_list_page(p) for p in range(page, batch_end)]
            batch_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            any_results = False
            for res in batch_results:
                if isinstance(res, Exception) or not res:
                    continue
                case_links.extend(res)
                any_results = True
                
            if any_results:
                empty_streak = 0
                if on_collection_progress:
                    on_collection_progress(len(case_links))
            else:
                empty_streak += 1
                if empty_streak >= max_empty_retries:
                    logger.warning(
                        "sudact.pagination_stopped: %d empty batches in a row at page %d",
                        empty_streak, page
                    )
                    break
                # Back off before retrying
                await asyncio.sleep(1.0)
                
            page = batch_end

        # Deduplicate links to prevent redundant fetching and duplication in Excel
        seen_links = set()
        unique_links = []
        for link, title, case_number in case_links:
            if link not in seen_links:
                seen_links.add(link)
                unique_links.append((link, title, case_number))
        case_links = unique_links

        case_links = case_links[:max_cases]
        case_id_collection_ms = int((time.perf_counter() - collect_start) * 1000)

        log_event(logger, "sudact.collected", count=len(case_links), params_article=params.article)

        if on_stage_change:
            on_stage_change("analyzing")

        # Phase 2: fetch individual decision pages concurrently
        details_start = time.perf_counter()
        decisions: list[CaseDecision] = []
        attempted = 0
        successful = 0

        detail_sem = asyncio.Semaphore(self._concurrency)

        async def fetch_one(item: tuple[str, str, str]) -> CaseDecision | None:
            nonlocal attempted
            relative_url, title, case_number = item
            full_url = SUDACT_BASE + relative_url if relative_url.startswith("/") else relative_url
            async with detail_sem:
                try:
                    html = await self._fetch_page_html(full_url)
                except Exception as exc:
                    log_event(logger, "sudact.detail_error", url=full_url, error=str(exc))
                    return None
            if not html:
                return None
            return self._parse_decision_page(html, full_url, title, case_number)

        tasks_detail = [asyncio.create_task(fetch_one(item)) for item in case_links]
        for coro in asyncio.as_completed(tasks_detail):
            if should_cancel and should_cancel():
                for t in tasks_detail:
                    t.cancel()
                break
            attempted += 1
            try:
                decision = await coro
            except Exception:
                decision = None
            if on_progress:
                on_progress(attempted)
            if decision is not None:
                decisions.append(decision)
                successful += 1
                if on_successful:
                    on_successful(successful)

        details_fetch_ms = int((time.perf_counter() - details_start) * 1000)

        # NOTE: LLM classification intentionally removed from Stage A (retrieval).
        # All collected decisions are passed to Stage B (pipeline.py) for validation.
        # This prevents 100% case rejection when LLM is overly strict or unavailable.

        log_event(
            logger, "sudact.fetch_done",
            attempted=attempted, successful=successful,
            collect_ms=case_id_collection_ms, details_ms=details_fetch_ms,
        )

        return FetchDecisionsResult(
            decisions=decisions,
            stats=FetchStats(
                attempted_cases=attempted,
                successful_cases=successful,
                retry_count=0,
                effective_concurrency=self._concurrency,
                case_id_collection_ms=case_id_collection_ms,
                details_fetch_ms=details_fetch_ms,
                filtered_by_court=0,
                court_compared_cases=0,
                total_pages=page - 1,
                total_cases_found=len(case_links),
            ),
            params=params,
        )

    # ------------------------------------------------------------------
    # URL building
    # ------------------------------------------------------------------

    def _build_page_url(self, params: SearchParams, page: int) -> str:
        """Build a sudact.ru search URL for the given params and page number."""
        prefix = self._PARAM_PREFIX
        base = f"{SUDACT_BASE}{self._CATEGORY_URL}?page={page}"

        date_from = _normalize_date_str(params.date_from)
        date_to = _normalize_date_str(params.date_to)

        if date_from:
            base += f"&{prefix}-date_from={date_from}"
        if date_to:
            base += f"&{prefix}-date_to={date_to}"

        # Add case type if specified (e.g., 'B' for bankruptcy)
        if params.case_type:
            base += f"&{prefix}-casetype={params.case_type}"

        # Build text search term from INN/name and Article
        search_terms = []
        if params.full_article:
            import urllib.parse
            # Use full string for lawchunkinfo, it's highly specific
            base += f"&{prefix}-lawchunkinfo={urllib.parse.quote_plus(params.full_article)}"
            # Include ONLY the article number in text search for best retrieval
            # (Adding the law name in txt too can often cause 0 results or timeout on Sudact)
            if params.article:
                search_terms.append(params.article)
        elif params.article:
            import urllib.parse
            base += f"&{prefix}-lawchunkinfo={urllib.parse.quote_plus(params.article)}"
            search_terms.append(params.article)
        elif params.law_display_name:
            search_terms.append(params.law_display_name)
        elif params.law_family == "127-ФЗ":
            search_terms.append("банкротство")
            
        if params.inn_or_name and not params.inn_type:  # name, not numeric INN
            search_terms.append(params.inn_or_name)
        if params.issue_phrase:
            search_terms.append(params.issue_phrase)
            
        if search_terms:
            import urllib.parse
            # Filter unique terms and join
            unique_terms = []
            seen = set()
            for t in search_terms:
                if t and t not in seen:
                    unique_terms.append(t)
                    seen.add(t)
            if unique_terms:
                base += f"&{prefix}-txt={urllib.parse.quote_plus(' '.join(unique_terms))}"

        # Court name filter
        if params.court:
            import urllib.parse
            base += f"&{prefix}-court={urllib.parse.quote_plus(params.court)}"

        log_event(logger, "sudact.build_url", url=base)
        return base

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _fetch_page_html(self, url: str) -> BeautifulSoup | None:
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept-Language": "ru-RU,ru;q=0.9",
            }
            resp = await self._client.get(url, headers=headers, follow_redirects=True)
            if resp.status_code != 200:
                log_event(logger, "fetch_page.error", url=url, status=resp.status_code)
                return None

            if "doc_ajax" in url:
                try:
                    # Initial JSON parse
                    data = resp.json()
                    
                    # Polling logic for asynchronous search
                    retries = 0
                    while data.get("status") in {"new", "started", None} and retries < 15:
                        if data.get("status") is None and "total_found" in data:
                            break # It might be done despite missing status
                        
                        await asyncio.sleep(1.0)
                        retries += 1
                        resp = await self._client.get(url, headers=headers, follow_redirects=True)
                        if resp.status_code != 200:
                            break
                        data = resp.json()
                        if data.get("status") == "finished":
                            break

                    html_content = data.get("content", "")
                    total_found = data.get("total_found")
                    total_found = data.get("total_found")
                    if not total_found or "Документы не найдены" in total_found:
                        log_event(logger, "sudact.no_docs_found", url=url)
                        return None
                    
                    # Wrap content in a body to ensure BS4 is robust
                    full_html = f"<html><body>{html_content}<div id='injected-total-found'>{total_found}</div></body></html>"
                    return BeautifulSoup(full_html, "lxml")
                except ValueError:
                    log_event(logger, "fetch_page.json_invalid", url=url)
                    return BeautifulSoup(resp.text, "lxml")
            
            return BeautifulSoup(resp.text, "lxml")
        except httpx.TimeoutException:
            log_event(logger, "sudact.timeout", url=url)
            return None
        except Exception as exc:
            log_event(logger, "sudact.fetch_error", url=url, error=str(exc))
            return None

    # ------------------------------------------------------------------
    # HTML parsing
    # ------------------------------------------------------------------

    def _extract_total_count_text(self, html: BeautifulSoup) -> int | None:
        """Try to extract the total result count displayed by sudact.ru."""
        # sudact shows something like "Найдено: 1 234 решения"
        for tag in html.find_all(string=re.compile(r"[Нн]айден")):
            m = re.search(r"(\d[\d\s\xa0]*)", str(tag))
            if m:
                try:
                    return int(m.group(1).replace(" ", "").replace("\xa0", ""))
                except ValueError:
                    pass
        return None

    def _extract_list_items(
        self, html: BeautifulSoup
    ) -> list[tuple[str, str, str]]:
        """Extract (relative_url, title, case_number) from a search result page."""
        results = (
            html.find("ul", class_="results2") 
            or html.find("ul", class_="results") 
            or html.find("div", class_="results") 
            or html.find("div", id="result-list")
        )
        if not results:
            return []
        items: list[tuple[str, str, str]] = []
        for li in results.find_all("li"):
            a = li.find("a")
            if not a:
                continue
            href = a.get("href", "")
            title = a.get_text(" ", strip=True)
            # Extract case number from title after №
            parts = title.split("№")
            case_number = parts[-1].strip() if len(parts) > 1 else ""
            items.append((href, title, case_number))
        return items

    def _parse_decision_page(
        self,
        html,
        url: str,
        title: str,
        case_number: str,
    ) -> CaseDecision | None:
        container = html.find("td", class_="h-col1 h-col1-inner3")
        if not container:
            # Fallback: try the main content area
            container = html.find("div", class_="entry") or html.find("article")
        if not container:
            return None

        full_text = container.get_text("\n")
        judge = _extract_judge(full_text)
        article = _extract_article(container)
        outcome = self._outcome_mapper.map_outcome(full_text)
        decision_date = _parse_date_from_title(title)

        # Prefer structured court name from the full HTML page, then fall back to place
        court_name = _extract_court_from_page(html)
        if not court_name:
            place = _extract_place(full_text)
            court_name = f"Арбитражный суд г. {place}" if place else "Арбитражный суд"
        place = _extract_place(full_text)  # keep raw_place for export

        # Extract act title from first line
        first_line = full_text.split("\n")[0].strip()
        act_match = re.match(r"^(Решение|Постановление|Определение)", first_line)
        act_title = act_match.group(1) if act_match else ""

        return CaseDecision(
            case_number=case_number,
            decision_date=decision_date,
            outcome=outcome,
            reasons=("оценка обстоятельств дела",),
            case_id=url,
            court_name=court_name,
            case_link=url,
            analysis_text=full_text[:15000],  # truncate to avoid memory issues
            decisive_act_title=act_title or first_line[:60].strip(),
            decisive_act_type=(
                "merits_act" if act_title in ("Решение", "Постановление") else "other"
            ),
            raw_number="",
            raw_date=decision_date,
            raw_case_number=case_number,
            raw_place=place or "",
            raw_judge=judge or "",
            raw_url=url,
            raw_article=article or "",
            raw_text=full_text,
        )

    # ------------------------------------------------------------------
    # LLM refinement
    # ------------------------------------------------------------------

    async def _refine_params_with_llm(
        self, query_text: str, params: SearchParams
    ) -> SearchParams:
        if self._llm_reason_extractor is None or not self._llm_reason_extractor.is_functional:
            return params
        try:
            llm_params = await self._llm_reason_extractor.parse_query(query_text)
        except Exception:
            return params

        year = llm_params.get("year") or (
            params.date_from[:4] if params.date_from else None
        )
        llm_quarter_raw = llm_params.get("quarter")
        llm_quarter: int | None = (
            int(llm_quarter_raw)
            if llm_quarter_raw is not None and str(llm_quarter_raw).isdigit()
            else None
        )
        final_quarter = params._regex_quarter or llm_quarter

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
            normalized = self._parser._extract_court(str(llm_court))
            final_court = normalized or str(llm_court)
        else:
            final_court = params.court

        return replace(
            params,
            article=llm_params.get("article") or params.article,
            full_article=llm_params.get("full_article") or params.full_article,
            court=final_court,
            date_from=date_from or params.date_from,
            date_to=date_to or params.date_to,
        )

    def _empty_result(self, params: SearchParams) -> FetchDecisionsResult:
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
            params=params,
        )
