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
        """Extract search parameters from a natural language query using LLM.

        Returns dict with 'article', 'court', 'year', 'quarter', 'case_type'.
        """
        if not query_text or not query_text.strip():
            return {"article": None, "court": None, "year": None, "quarter": None, "case_type": None}
        try:
            prompt = (
                f"Ты — эксперт по поиску в судебных базах данных (КАД Арбитр).\n"
                f"Твоя задача — извлечь параметры поиска из запроса пользователя для API.\n\n"
                f"Запрос: {query_text}\n\n"
                f"Извлеки следующие поля в формате JSON:\n"
                f"1. \"article\": только номер статьи (например, \"61.2\" или \"723\").\n"
                f"2. \"full_article\": номер статьи с названием кодекса/закона для поиска (например, \"ст. 723 ГК РФ\", \"ст. 61.2 закона о банкротстве\").\n"
                f"3. \"court\": официальное название суда или его часть (например, \"АС города Москвы\", \"15 ААС\").\n"
                f"4. \"year\": год в формате ГГГГ.\n"
                f"5. \"quarter\": номер квартала (1, 2, 3 или 4).\n"
                f"6. \"case_type\": тип дела - \"Б\" (банкротство), \"Г\" (гражданское), \"А\" (административное) или null.\n\n"
                f"ПРАВИЛА:\n"
                f"- В поле \"full_article\" ДОБАВЛЯЙ аббревиатуру кодекса (ГК, НК, УК) или краткое название закона, если они упомянуты или понятны из контекста.\n"
                f"- Если статья из ГК РФ или других кодексов (кроме Закона о банкротстве), ставь case_type: \"Г\".\n"
                f"- Если в запросе есть слово \"банкротство\" или ст. 61.2, 61.3, 127-ФЗ, ставь case_type: \"Б\".\n"
                f"- Если параметр не указан, ставь null.\n\n"
                f"Верни ТОЛЬКО чистый JSON."
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
            parsed = json.loads(content)
            self._logger.info("llm.parse_query.v2", extra={"data": parsed})
            return {
                "article": str(parsed.get("article")) if parsed.get("article") else None,
                "full_article": str(parsed.get("full_article")) if parsed.get("full_article") else None,
                "court": parsed.get("court"),
                "year": str(parsed.get("year")) if parsed.get("year") else None,
                "quarter": str(parsed.get("quarter")) if parsed.get("quarter") else None,
                "case_type": parsed.get("case_type"),
            }
        except Exception as exc:
            self._logger.warning("llm.parse_query_failed", extra={"data": {"error": str(exc)}})
        return {"article": None, "full_article": None, "court": None, "year": None, "quarter": None, "case_type": None}

    async def generate_summary(
        self, 
        court: str, 
        period: str, 
        article: str | None, 
        total: int, 
        satisfied: int, 
        denied: int, 
        unknown: int,
        top_satisfied_reasons: list[str],
        top_denied_reasons: list[str]
    ) -> str:
        """Generate a professional legal summary of the analysis results."""
        prompt = (
            f"Сформулируй краткую и профессиональную правовую сводку на основе статистики судебных дел.\n\n"
            f"Параметры запроса:\n"
            f"- Суд: {court}\n"
            f"- Период: {period}\n"
            f"- Статья: {article if article else 'не указана'}\n\n"
            f"Статистика:\n"
            f"- Всего дел: {total}\n"
            f"- Удовлетворено: {satisfied} ({round(satisfied/total*100 if total>0 else 0)}%)\n"
            f"- Отказано: {denied} ({round(denied/total*100 if total>0 else 0)}%)\n"
            f"- Не определено: {unknown} ({round(unknown/total*100 if total>0 else 0)}%)\n\n"
            f"Типовые основания для удовлетворения:\n"
            + "\n".join(f"- {r}" for r in top_satisfied_reasons) + "\n\n"
            f"Типовые основания для отказа:\n"
            + "\n".join(f"- {r}" for r in top_denied_reasons) + "\n\n"
            f"Твоя задача — написать 3-4 предложения, которые резюмируют практику. "
            f"Избегай шаблонных фраз вроде 'Решение суда удовлетворяет иск'. "
            f"Пиши как опытный юрист для другого юриста. Акцентируй внимание на шансах успеха.\n"
            f"Начни сразу с текста сводки."
        )
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
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.5,
                    "max_tokens": 400,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            summary_text = response.json()["choices"][0]["message"]["content"].strip()
            
            header = (
                f"⚖️ СВОДКА ПРАКТИКИ:\n"
                f"📍 Суд: {court} | 📅 Период: {period}\n"
                f"📝 Статья: {article if article else '—'} | 📊 Всего дел: {total}\n"
                f"✅ Удовл: {satisfied} | ❌ Отказ: {denied} (не опред: {unknown})\n\n"
            )
            return header + summary_text
        except Exception as exc:
            self._logger.warning("llm.generate_summary_failed", extra={"data": {"error": str(exc)}})
            # Fallback to simple template
            return f"Суд: {court}\nВсего дел: {total}\nУдовлетворено: {satisfied}\nОтказано: {denied}"

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
            f"Ты — высококвалифицированный юрист Арбитражных судов РФ.\n\n"
            f"{query_hint}"
            f"Контекст из системы КАД Арбитр:\n{context}\n"
            f"Исход дела: {outcome_ru}\n\n"
            f"ТВОЯ ЗАДАЧА:\n"
            f"1. Проверь, относится ли этот судебный акт к спору по статье {article}.\n"
            f"2. Если акт релевантен, извлеки 1-3 ключевых правовых тезиса.\n\n"
            f"КРИТЕРИИ РЕЛЕВАНТНОСТИ:\n"
            f"- ТАК КАК ПОИСК УЖЕ БЫЛ ВЫПОЛНЕН ПО СТАТЬЕ {article}, СЧИТАЙ АКТ РЕЛЕВАНТНЫМ, если краткий контекст не содержит явных противоречий (например, упоминания совсем другой статьи или категории спора).\n"
            f"- Если это процедурное определение (назначение дела, перенос) БЕЗ упоминания сути — ВСЕ РАВНО ОТМЕЧАЙ КАК РЕЛЕВАНТНОЕ (это часть спора по нужной статье).\n"
            f"- Только если из текста очевидно, что спор касается ДРУГИХ правоотношений — ставь НЕ_РЕЛЕВАНТНО.\n\n"
            f"ФОРМАТ ОТВЕТА:\n"
            f"- Если релевантно: JSON массив строк. Если в тексте нет оснований для анализа, пиши [\"оценка обстоятельств дела\"].\n"
            f"- Если НЕ релевантно (явное несовпадение): JSON массив [\"НЕ_РЕЛЕВАНТНО\"].\n\n"
            f"Ответь ТОЛЬКО JSON-массивом."
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
