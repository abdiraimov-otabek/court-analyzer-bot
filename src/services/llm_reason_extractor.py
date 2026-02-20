from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import httpx

from src.domain.entities import CaseDecision, CaseOutcome


class LLMReasonExtractor:
    """Extracts legal grounds from Russian arbitration court events using an LLM.

    Used as a fallback when regex-based ReasonExtractor returns the generic
    "оценка обстоятельств дела" label. Calls OpenRouter chat completions API
    with a structured prompt and validates the response against a canonical list.
    """

    _CANONICAL_LABELS = [
        "неравноценное встречное исполнение (п.1 ст.61.2)",
        "причинение вреда кредиторам (п.2 ст.61.2)",
        "подозрительность сделки (ст.61.2)",
        "сделка с предпочтением (ст.61.3)",
        "нарушение очередности удовлетворения требований",
        "злоупотребление правом (ст.10 ГК)",
        "мнимость сделки (ст.170 ГК)",
        "притворность сделки (ст.170 ГК)",
        "ничтожность сделки (ст.168 ГК)",
        "пропуск срока исковой давности",
        "недоказанность обстоятельств",
        "недостаточность доказательств",
        "необоснованность требований",
        "отсутствие правовых оснований",
        "осведомленность контрагента о банкротстве",
        "аффилированность сторон",
        "заинтересованность контрагента",
        "признаки неплатежеспособности должника",
        "добросовестность контрагента",
        "безвозмездность сделки",
        "оспаривание сделки по ст.61.2 Закона о банкротстве",
        "оспаривание сделки по ст.61.3 Закона о банкротстве",
        "ненадлежащий ответчик",
        "нарушение обязанностей арбитражного управляющего",
        "применение последствий недействительности сделки",
        "крупная сделка",
    ]

    _FALLBACK: tuple[str, ...] = ("оценка обстоятельств дела",)
    _NOT_RELEVANT: tuple[str, ...] = ("НЕ_РЕЛЕВАНТНО",)
    _CANONICAL_SET: frozenset[str] = frozenset(_CANONICAL_LABELS)
    _MAX_RETRIES = 1

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_key: str,
        model: str = "openai/gpt-4o-mini",
        timeout: int = 15,
        max_concurrent: int = 8,
    ) -> None:
        self._http_client = http_client
        self._api_key = api_key
        self._model = model
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._cache: dict[str, tuple[str, ...]] = {}
        self._classify_cache: dict[str, tuple[bool, tuple[str, ...]]] = {}
        self._budget_remaining: int | None = None
        self._logger = logging.getLogger("llm_reason_extractor")

    def set_fetch_budget(self, max_calls: int) -> None:
        """Cap LLM calls for this fetch batch to avoid surprise costs."""
        self._budget_remaining = max_calls

    def reset_fetch_budget(self) -> None:
        self._budget_remaining = None

    async def parse_query(self, query_text: str) -> dict[str, str | None]:
        """Extract article number from a natural language query using LLM.

        Returns dict with 'article' key (str or None).
        """
        if not query_text or not query_text.strip():
            return {"article": None}
        try:
            response = await self._http_client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://github.com/kad-bot",
                    "X-Title": "KAD Bot",
                },
                json={
                    "model": self._model,
                    "messages": [{"role": "user", "content": (
                        f"Извлеки номер статьи закона из запроса пользователя.\n\n"
                        f"Запрос: {query_text}\n\n"
                        f"Верни ТОЛЬКО JSON: {{\"article\": \"номер\"}} или {{\"article\": null}} "
                        f"если статья не указана.\n"
                        f"Примеры:\n"
                        f"\"практика по ст 61.2\" -> {{\"article\": \"61.2\"}}\n"
                        f"\"статье 60 закона о банкротстве\" -> {{\"article\": \"60\"}}\n"
                        f"\"723 ГК РФ\" -> {{\"article\": \"723\"}}\n"
                        f"\"АС Москвы 2024\" -> {{\"article\": null}}"
                    )}],
                    "temperature": 0,
                    "max_tokens": 30,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"].strip()
            if content.startswith("```"):
                content = content.split("```", 2)[1]
                if content.startswith("json"):
                    content = content[4:]
                content = content.strip()
            parsed = json.loads(content)
            if isinstance(parsed, dict) and "article" in parsed:
                val = parsed["article"]
                self._logger.info("llm.parse_query", extra={"data": {"article": val}})
                return {"article": str(val) if val else None}
        except Exception as exc:
            self._logger.warning("llm.parse_query_failed", extra={"data": {"error": str(exc)}})
        return {"article": None}

    async def extract(self, text: str, outcome: CaseOutcome) -> tuple[str, ...]:
        if not text or not text.strip():
            return self._FALLBACK
        cache_key = hashlib.sha256(f"{outcome.value}:{text}".encode()).hexdigest()
        if cache_key in self._cache:
            self._logger.debug("llm.cache_hit")
            return self._cache[cache_key]
        if self._budget_remaining is not None:
            if self._budget_remaining <= 0:
                self._logger.debug("llm.budget_exhausted")
                return self._FALLBACK
            self._budget_remaining -= 1
        try:
            async with self._semaphore:
                result = await self._call_with_retry(text, outcome)
            self._cache[cache_key] = result
            self._logger.info(
                "llm.extracted",
                extra={"data": {"outcome": outcome.value, "labels": list(result)}},
            )
            return result
        except Exception as exc:
            self._logger.warning("llm.failed", extra={"data": {"error": str(exc)}})
            return self._FALLBACK  # never let LLM failures crash the pipeline

    async def classify_and_extract(
        self, decision: CaseDecision, article: str, query_text: str = "",
    ) -> tuple[bool, tuple[str, ...]]:
        """Classify whether a case is relevant to a specific article and extract reasons.

        Returns (is_relevant, reasons). When irrelevant, reasons = ("НЕ_РЕЛЕВАНТНО",).
        """
        context = self._build_case_context(decision)
        if not context.strip():
            return False, self._NOT_RELEVANT
        cache_key = hashlib.sha256(
            f"classify:{article}:{query_text}:{decision.outcome.value}:{decision.case_id}:{context}".encode()
        ).hexdigest()
        if cache_key in self._classify_cache:
            self._logger.debug("llm.classify_cache_hit")
            return self._classify_cache[cache_key]
        if self._budget_remaining is not None:
            if self._budget_remaining <= 0:
                self._logger.debug("llm.budget_exhausted")
                return True, self._FALLBACK  # keep case when budget exhausted
            self._budget_remaining -= 1
        try:
            async with self._semaphore:
                result = await self._call_classify_with_retry(context, decision.outcome, article, query_text)
            self._classify_cache[cache_key] = result
            self._logger.info(
                "llm.classified",
                extra={"data": {
                    "article": article,
                    "case_number": decision.case_number,
                    "relevant": result[0],
                    "labels": list(result[1]),
                }},
            )
            return result
        except Exception as exc:
            self._logger.warning("llm.classify_failed", extra={"data": {"error": str(exc)}})
            return True, self._FALLBACK  # on error, keep the case (safe default)

    @staticmethod
    def _build_case_context(decision: CaseDecision) -> str:
        """Build rich context string from all available case data."""
        parts: list[str] = []
        if decision.case_number:
            parts.append(f"Дело: {decision.case_number}")
        if decision.court_name:
            parts.append(f"Суд: {decision.court_name}")
        if decision.case_category:
            cat_map = {"Б": "Банкротство", "Г": "Гражданское", "А": "Административное"}
            parts.append(f"Категория: {cat_map.get(decision.case_category, decision.case_category)}")
        if decision.analysis_text:
            parts.append(f"Текст события: {decision.analysis_text}")
        if decision.reasons and decision.reasons != ("оценка обстоятельств дела",):
            parts.append(f"Извлеченные основания: {', '.join(decision.reasons)}")
        return "\n".join(parts)

    async def _call_classify_with_retry(
        self, text: str, outcome: CaseOutcome, article: str, query_text: str = "",
    ) -> tuple[bool, tuple[str, ...]]:
        last_exc: Exception = RuntimeError("no attempts")
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                return await self._call_classify_api(text, outcome, article, query_text)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {429, 503} and attempt < self._MAX_RETRIES:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    last_exc = exc
                    continue
                raise
        raise last_exc

    async def _call_classify_api(
        self, context: str, outcome: CaseOutcome, article: str, query_text: str = "",
    ) -> tuple[bool, tuple[str, ...]]:
        outcome_ru = {"satisfied": "Удовлетворено", "denied": "Отказано"}.get(
            outcome.value, "Не определено"
        )
        query_hint = f"Запрос пользователя: {query_text}\n" if query_text else ""
        prompt = (
            f"Ты юрист-аналитик.\n\n"
            f"{query_hint}"
            f"{context}\n"
            f"Результат: {outcome_ru}\n\n"
            f"Задача: определи, относится ли данный судебный акт к ст.{article}.\n\n"
            f"ВАЖНО: Дело найдено поисковой системой КАД по запросу, содержащему ст.{article}. "
            f"Текст события — это краткая карточка судебного акта, которая часто НЕ содержит "
            f"номер статьи напрямую. Это нормально.\n\n"
            f"ПРАВИЛО: По умолчанию дело РЕЛЕВАНТНО (доверяем поиску КАД).\n"
            f"Считай НЕ релевантным ТОЛЬКО если из контекста ЯВНО видно, что дело "
            f"относится к совершенно другой теме (например, чисто налоговый спор, "
            f"трудовой спор, или дело вообще не связано с банкротством, хотя "
            f"категория указана как банкротство).\n\n"
            f"Чисто процедурные действия в рамках банкротного дела (принятие заявления, "
            f"подготовка к заседанию, определения и решения) — считай РЕЛЕВАНТНЫМИ, "
            f"если категория дела — банкротство.\n\n"
            f"Если РЕЛЕВАНТНО — верни 1–3 коротких правовых основания (свободный текст).\n"
            f'Если НЕ РЕЛЕВАНТНО — верни ["НЕ_РЕЛЕВАНТНО"].\n\n'
            f"Ответь ТОЛЬКО JSON-массивом строк, без пояснений."
        )
        response = await self._http_client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/kad-bot",
                "X-Title": "KAD Bot",
            },
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 150,
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        raw = json.loads(content)
        if not isinstance(raw, list):
            return True, self._FALLBACK
        # Check if LLM says not relevant
        if any(r == "НЕ_РЕЛЕВАНТНО" for r in raw):
            return False, self._NOT_RELEVANT
        valid = tuple(r for r in raw if isinstance(r, str) and r.strip())
        return True, (valid if valid else self._FALLBACK)

    async def _call_with_retry(self, text: str, outcome: CaseOutcome) -> tuple[str, ...]:
        last_exc: Exception = RuntimeError("no attempts")
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                return await self._call_api(text, outcome)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in {429, 503} and attempt < self._MAX_RETRIES:
                    await asyncio.sleep(1.0 * (attempt + 1))
                    last_exc = exc
                    continue
                raise
        raise last_exc

    async def _call_api(self, text: str, outcome: CaseOutcome) -> tuple[str, ...]:
        outcome_ru = {"satisfied": "Удовлетворено", "denied": "Отказано"}.get(
            outcome.value, "Не определено"
        )
        labels_str = "\n".join(f"- {label}" for label in self._CANONICAL_LABELS)
        prompt = (
            f"Ты анализируешь определение арбитражного суда по делу о банкротстве.\n"
            f"Текст события: {text}\n"
            f"Результат: {outcome_ru}\n\n"
            f"Выбери 1–3 правовых основания из списка ниже, которые наиболее точно "
            f"соответствуют данному делу. Если информации недостаточно, верни "
            f'["оценка обстоятельств дела"].\n\n'
            f"Допустимые значения:\n{labels_str}\n\n"
            f"Ответь ТОЛЬКО JSON-массивом строк, без пояснений."
        )
        response = await self._http_client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/kad-bot",
                "X-Title": "KAD Bot",
            },
            json={
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": 150,
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        # GPT-4o-mini sometimes wraps output in ```json ... ``` fences
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("json"):
                content = content[4:]
            content = content.strip()
        raw = json.loads(content)
        if not isinstance(raw, list):
            return self._FALLBACK
        valid = tuple(
            r for r in raw
            if r in self._CANONICAL_SET or r == self._FALLBACK[0]
        )
        return valid if valid else self._FALLBACK
