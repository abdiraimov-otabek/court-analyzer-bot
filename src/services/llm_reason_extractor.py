from __future__ import annotations

import asyncio
import hashlib
import json
import logging

import httpx
from pydantic import BaseModel, Field, ValidationError

from src.domain.entities import CaseDecision, CaseOutcome


class ClassifyResultItem(BaseModel):
    relevant: bool = True
    reasons: list[str] = Field(default_factory=list)
    proof_quote: str = ""
    outcome: str = "NOT FOUND IN DOCUMENT"


class LLMReasonExtractor:
    """Extracts legal grounds from Russian arbitration court events using an LLM.

    Used as a fallback when regex-based ReasonExtractor returns the generic
    "оценка обстоятельств дела" label. Calls OpenRouter chat completions API
    with a structured prompt and validates the response against a canonical list.
    """

    _CANONICAL_LABELS = [
        # Bankruptcy-specific (ст.61.2, 61.3, Закон о банкротстве)
        "неравноценное встречное исполнение (п.1 ст.61.2)",
        "причинение вреда кредиторам (п.2 ст.61.2)",
        "подозрительность сделки (ст.61.2)",
        "сделка с предпочтением (ст.61.3)",
        "нарушение очередности удовлетворения требований",
        "осведомленность контрагента о банкротстве",
        "аффилированность сторон",
        "заинтересованность контрагента",
        "признаки неплатежеспособности должника",
        "добросовестность контрагента",
        "безвозмездность сделки",
        "оспаривание сделки по ст.61.2 Закона о банкротстве",
        "оспаривание сделки по ст.61.3 Закона о банкротстве",
        "нарушение обязанностей арбитражного управляющего",
        "применение последствий недействительности сделки",
        "субсидиарная ответственность",
        "включение в реестр требований кредиторов",
        "исключение из реестра требований кредиторов",
        "утверждение мирового соглашения",
        "отказ в утверждении мирового соглашения",
        "жалоба на действия арбитражного управляющего",
        "оспаривание торгов",
        "распределение конкурсной массы",
        # General civil law
        "злоупотребление правом (ст.10 ГК)",
        "мнимость сделки (ст.170 ГК)",
        "притворность сделки (ст.170 ГК)",
        "ничтожность сделки (ст.168 ГК)",
        "пропуск срока исковой давности",
        "недоказанность обстоятельств",
        "недостаточность доказательств",
        "необоснованность требований",
        "отсутствие правовых оснований",
        "ненадлежащий ответчик",
        "крупная сделка",
        "нарушение договорных обязательств",
        "ненадлежащее исполнение обязательств",
        "взыскание задолженности",
        "взыскание убытков",
        "взыскание неустойки",
        "неосновательное обогащение",
        "признание права собственности",
        "расторжение договора",
        "возмещение ущерба",
        "нарушение условий договора",
        "ненадлежащее качество работ",
        "нарушение сроков исполнения",
        # Administrative
        "нарушение антимонопольного законодательства",
        "административное правонарушение",
        "оспаривание решения государственного органа",
        "оспаривание ненормативного правового акта",
        "налоговое правонарушение",
    ]

    _FALLBACK: tuple[str, ...] = ("оценка обстоятельств дела",)
    _NOT_RELEVANT: tuple[str, ...] = ("НЕ_РЕЛЕВАНТНО",)
    _CANONICAL_SET: frozenset[str] = frozenset(_CANONICAL_LABELS)
    _MAX_RETRIES = 1

    def __init__(
        self,
        http_client: httpx.AsyncClient,
        api_key: str,
        model: str = "anthropic/claude-3.5-sonnet",
        fast_model: str = "google/gemini-2.5-flash",
        timeout: int = 15,
        max_concurrent: int = 8,
    ) -> None:
        self._http_client = http_client
        self._api_key = api_key
        self._model = model
        self._fast_model = fast_model
        self._timeout = timeout
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._cache: dict[str, tuple[str, ...]] = {}
        self._classify_cache: dict[
            str, tuple[bool, tuple[str, ...], str, str | None]
        ] = {}
        self._outcome_cache: dict[str, tuple[tuple[str, ...], str | None]] = {}
        self._MAX_CACHE_SIZE = 2000
        self._budget_remaining: int | None = None
        self._logger = logging.getLogger("llm_reason_extractor")

    def _clean_llm_json(self, content: str) -> str:
        """Extract and clean JSON string from LLM response."""
        content = content.strip()
        # Handle markdown code blocks
        if "```" in content:
            try:
                # Try to find content between first and last ```
                parts = content.split("```")
                # Parts are: [text, block1, text, block2, text]
                # Usually it's in the second part if there's only one block
                for i in range(1, len(parts), 2):
                    block = parts[i].strip()
                    if block.startswith("json"):
                        block = block[4:].strip()
                    if block:
                        return block
            except Exception:
                pass

        # If no code block or split failed, try to find first '[' or '{'
        start_idx = -1
        for i, char in enumerate(content):
            if char in "[{":
                start_idx = i
                break

        if start_idx != -1:
            end_idx = -1
            target = "]" if content[start_idx] == "[" else "}"
            for i in range(len(content) - 1, start_idx, -1):
                if content[i] == target:
                    end_idx = i
                    break
            if end_idx != -1:
                return content[start_idx : end_idx + 1]

        return content

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
            return {
                "article": None,
                "court": None,
                "year": None,
                "quarter": None,
                "case_type": None,
            }
        try:
            prompt = (
                f"Ты — эксперт по поиску в судебных базах данных (КАД Арбитр).\n"
                f"Твоя задача — извлечь параметры поиска из запроса пользователя для API.\n\n"
                f"Запрос: {query_text}\n\n"
                f"Извлеки следующие поля в формате JSON:\n"
                f'1. "article": только номер статьи (например, "61.2" или "723").\n'
                f'2. "full_article": номер статьи с названием кодекса/закона для поиска (например, "ст. 723 ГК РФ", "ст. 61.2 закона о банкротстве").\n'
                f'3. "court": официальное название суда или его часть (например, "АС города Москвы", "15 ААС").\n'
                f'4. "year": год в формате ГГГГ.\n'
                f'5. "quarter": номер квартала (1, 2, 3 или 4).\n'
                f'6. "case_type": тип дела - "Б" (банкротство), "Г" (гражданское), "А" (административное) или null.\n'
                f'7. "date_from": конкретная дата начала в формате ГГГГ-ММ-ДД (если указан месяц, например "март 2024" -> "2024-03-01").\n'
                f'8. "date_to": конкретная дата конца в формате ГГГГ-ММ-ДД (если указан месяц, например "март 2024" -> "2024-03-31").\n\n'
                f"ПРАВИЛА:\n"
                f'- В поле "full_article" ДОБАВЛЯЙ аббревиатуру кодекса (ГК, НК, УК) или краткое название закона, если они упомянуты или понятны из контекста.\n'
                f'- Статьи 61.1, 61.2, 61.3, 61.4, 61.6–61.9, 100, 134, 138, 142, 213.11, 213.32 — это статьи Закона о банкротстве (127-ФЗ). Для них ставь case_type: "Б" и в full_article пиши "ст. X Закона о банкротстве".\n'
                f'- Если в запросе есть слово "банкрот", "несостоятельност" или упомянут 127-ФЗ — ставь case_type: "Б".\n'
                f'- Если статья из ГК РФ, НК РФ или других кодексов (не из Закона о банкротстве) — ставь case_type: "Г".\n'
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
                    "model": self._fast_model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0,
                    "max_tokens": 150,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            content = self._clean_llm_json(content)
            parsed = json.loads(content)
            self._logger.info("llm.parse_query.v2", extra={"data": parsed})
            return {
                "article": str(parsed.get("article"))
                if parsed.get("article")
                else None,
                "full_article": str(parsed.get("full_article"))
                if parsed.get("full_article")
                else None,
                "court": parsed.get("court"),
                "year": str(parsed.get("year")) if parsed.get("year") else None,
                "quarter": str(parsed.get("quarter"))
                if parsed.get("quarter")
                else None,
                "case_type": parsed.get("case_type"),
                "date_from": parsed.get("date_from"),
                "date_to": parsed.get("date_to"),
            }
        except Exception as exc:
            self._logger.warning(
                "llm.parse_query_failed", extra={"data": {"error": str(exc)}}
            )
        return {
            "article": None,
            "full_article": None,
            "court": None,
            "year": None,
            "quarter": None,
            "case_type": None,
            "date_from": None,
            "date_to": None,
        }

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
        top_denied_reasons: list[str],
        total_pages: int = 0,
        total_cases_found: int = 0,
        reason_confidence: float = 1.0,
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
            f"- Удовлетворено: {satisfied} ({round(satisfied / total * 100 if total > 0 else 0)}%)\n"
            f"- Отказано: {denied} ({round(denied / total * 100 if total > 0 else 0)}%)\n"
            f"- Не определено: {unknown} ({round(unknown / total * 100 if total > 0 else 0)}%)\n"
            f"- Масштаб выборки: {'обработано ВСЕ найдено' if total_pages <= 1 else f'обработано {total} из {total_cases_found} дел ({total_pages} стр.)'}\n\n"
            f"Типовые основания для удовлетворения:\n"
            + "\n".join(f"- {r}" for r in top_satisfied_reasons)
            + "\n\n"
            "Типовые основания для отказа:\n"
            + "\n".join(f"- {r}" for r in top_denied_reasons)
            + "\n\n"
            "Твоя задача — написать 3-4 предложения, которые резюмируют практику. "
            "Избегай шаблонных фраз вроде 'Решение суда удовлетворяет иск'. "
            "Пиши как опытный юрист для другого юриста. Акцентируй внимание на шансах успеха.\n"
            + (
                "ВАЖНО: Уверенность в извлечённых основаниях ниже средней — часть дел классифицирована без прямых цитат. Упомяни это в сводке.\n"
                if reason_confidence < 0.6
                else ""
            )
            + "Начни сразу с текста сводки."
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
                    "temperature": 0,
                    "max_tokens": 400,
                },
                timeout=self._timeout,
            )
            response.raise_for_status()
            summary_text = response.json()["choices"][0]["message"]["content"].strip()

            article_line = f"Статья: {article} | " if article else ""
            satisfied_pct = round(satisfied / total * 100) if total > 0 else 0
            denied_pct = round(denied / total * 100) if total > 0 else 0
            unknown_pct = round(unknown / total * 100) if total > 0 else 0
            quality_note = ""
            if reason_confidence < 0.6:
                quality_note = "⚠️ Низкая уверенность в основаниях: часть дел классифицирована без прямых цитат из судебных актов.\n\n"
            header = (
                f"СВОДКА ПО ЗАПРОСУ:\n"
                f"Суд: {court} | Период: {period} | {article_line}Всего дел: {total}\n"
                f"Статистика: Удовлетворено - {satisfied} ({satisfied_pct}%), "
                f"Отказано - {denied} ({denied_pct}%), "
                f"Не определено - {unknown} ({unknown_pct}%)\n"
                f"{quality_note}\n"
            )
            return header + summary_text
        except Exception as exc:
            self._logger.warning(
                "llm.generate_summary_failed", extra={"data": {"error": str(exc)}}
            )
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
            # item 1.7: Bounded Cache
            if len(self._cache) > self._MAX_CACHE_SIZE:
                # Simple eviction: clear 50 if full
                keys_to_remove = list(self._cache.keys())[:50]
                for k in keys_to_remove:
                    self._cache.pop(k, None)

            self._cache[cache_key] = result
            self._logger.info(
                "llm.extracted",
                extra={"data": {"outcome": outcome.value, "labels": list(result)}},
            )
            return result
        except Exception as exc:
            self._logger.warning("llm.failed", extra={"data": {"error": str(exc)}})
            return self._FALLBACK  # never let LLM failures crash the pipeline

    async def extract_with_outcome(
        self, decision: CaseDecision
    ) -> tuple[tuple[str, ...], str | None]:
        """Extract both legal grounds and outcome mapping for a SINGLE case.
        Unlike classify_batch, this is used for per-case enrichment."""
        ctx = self._build_case_context(decision)
        if not ctx:
            return self._FALLBACK, None

        cache_key = hashlib.sha256(
            f"{decision.outcome.value}:{ctx}".encode()
        ).hexdigest()
        if cache_key in self._outcome_cache:
            return self._outcome_cache[cache_key]

        if self._budget_remaining is not None:
            if self._budget_remaining <= 0:
                self._logger.debug(
                    "llm.budget_exhausted", extra={"method": "extract_with_outcome"}
                )
                return self._FALLBACK, None
            self._budget_remaining -= 1
        try:
            async with self._semaphore:
                reasons, outcome = await self._call_extract_with_outcome(
                    ctx, decision.outcome
                )

            if len(self._outcome_cache) > self._MAX_CACHE_SIZE:
                keys_to_remove = list(self._outcome_cache.keys())[:50]
                for k in keys_to_remove:
                    self._outcome_cache.pop(k, None)

            self._outcome_cache[cache_key] = (reasons, outcome)
            return reasons, outcome
        except Exception as exc:
            self._logger.warning(
                "llm.extract_with_outcome_failed",
                extra={"data": {"error": str(exc)}},
            )
            return self._FALLBACK, None

    async def _call_extract_with_outcome(
        self, text: str, outcome: CaseOutcome
    ) -> tuple[tuple[str, ...], str | None]:
        outcome_ru = {"satisfied": "Удовлетворено", "denied": "Отказано"}.get(
            outcome.value, "Не определено"
        )
        prompt = (
            f"Ты анализируешь судебный акт арбитражного суда.\n"
            f"Текст события: {text}\n"
            f"Текущий автоопределённый результат: {outcome_ru}\n\n"
            f"Задача 1: Выбери 1-3 правовых основания (например: 'взыскание задолженности', 'неустойка', 'оспаривание сделки').\n"
            f'Задача 2: Определи исход дела: "satisfied" (удовлетворено), "denied" (отказано), или "unknown".\n\n'
            f'Ответь JSON: {{"reasons": ["основание1"], "outcome": "satisfied"}}\n'
            f"Без пояснений, только JSON."
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
                "max_tokens": 200,
            },
            timeout=self._timeout,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"].strip()
        content = self._clean_llm_json(content)
        parsed = json.loads(content)

        if isinstance(parsed, list):
            # LLM returned list instead of dict — extract reasons from first element
            valid = tuple(r for r in parsed if isinstance(r, str) and r.strip())
            return (valid if valid else self._FALLBACK), None

        if isinstance(parsed, dict):
            reasons_raw = parsed.get("reasons", [])
            if not isinstance(reasons_raw, list):
                reasons_raw = [self._FALLBACK[0]]
            valid = tuple(r for r in reasons_raw if isinstance(r, str) and r.strip())
            raw_outcome = str(parsed.get("outcome", "") or "").strip().lower()
            llm_outcome: str | None = None
            if raw_outcome == "satisfied":
                llm_outcome = "satisfied"
            elif raw_outcome == "denied":
                llm_outcome = "denied"
            return (valid if valid else self._FALLBACK), llm_outcome

        return self._FALLBACK, None

    async def classify_and_extract(
        self,
        decision: CaseDecision,
        article: str,
        query_text: str = "",
    ) -> tuple[bool, tuple[str, ...], str, str | None]:
        """Compatibility wrapper for classify_batch."""
        results = await self.classify_batch([decision], article, query_text)
        return results[0]

    async def classify_batch(
        self,
        decisions: list[CaseDecision],
        article: str,
        query_text: str = "",
    ) -> list[tuple[bool, tuple[str, ...], str, str | None]]:
        """Classify a batch of decisions using batch LLM calls for efficiency.

        Returns list of (is_relevant, reasons, proof_quote, llm_outcome).
        llm_outcome is "satisfied"/"denied"/None — used when rule-based detection fails.
        """
        results: list[tuple[bool, tuple[str, ...], str, str | None] | None] = [
            None
        ] * len(decisions)
        to_fetch_indices: list[int] = []

        # 1. Check cache first
        for i, decision in enumerate(decisions):
            context = self._build_case_context(decision)
            if not context or not context.strip():
                results[i] = (False, self._NOT_RELEVANT, "", None)
                continue

            cache_key = hashlib.sha256(
                f"classify:v8:{article}:{query_text}:{decision.outcome.value}:{decision.case_id}:{context}".encode()
            ).hexdigest()

            if cache_key in self._classify_cache:
                results[i] = self._classify_cache[cache_key]
            else:
                to_fetch_indices.append(i)

        if not to_fetch_indices:
            return [res for res in results if res is not None]

        # 2. Process missing cases in sub-batches of 10
        batch_size = 10
        for i in range(0, len(to_fetch_indices), batch_size):
            chunk_indices = to_fetch_indices[i : i + batch_size]
            chunk_decisions = [decisions[idx] for idx in chunk_indices]

            if self._budget_remaining is not None:
                if self._budget_remaining <= 0:
                    for idx in chunk_indices:
                        # Fail-safe: if budget exhausted, pass through as relevant to avoid data loss
                        results[idx] = (
                            True,
                            (self._FALLBACK[0], "Capped by budget"),
                            "",
                            None,
                        )
                    continue
                self._budget_remaining -= 1  # Each batch counts as 1 call for budget

            try:
                async with self._semaphore:
                    batch_results = await self._call_classify_batch_api(
                        chunk_decisions, article, query_text
                    )

                for idx, res in zip(chunk_indices, batch_results):
                    results[idx] = res
                    # Cache individual result
                    decision = decisions[idx]
                    context = self._build_case_context(decision)
                    cache_key = hashlib.sha256(
                        f"classify:v8:{article}:{query_text}:{decision.outcome.value}:{decision.case_id}:{context}".encode()
                    ).hexdigest()
                    self._classify_cache[cache_key] = res

            except Exception as exc:
                self._logger.warning(
                    "llm.batch_classify_failed", extra={"data": {"error": str(exc)}}
                )
                for idx in chunk_indices:
                    # Fail-safe: if LLM fails, pass through as relevant to avoid 100% rejection
                    results[idx] = (
                        True,
                        (self._FALLBACK[0], f"LLM Error: {str(exc)[:50]}"),
                        "",
                        None,
                    )

        return [res for res in results if res is not None]

    async def _call_classify_batch_api(
        self,
        decisions: list[CaseDecision],
        article: str,
        query_text: str = "",
    ) -> list[tuple[bool, tuple[str, ...], str, str | None]]:
        items = []
        for i, d in enumerate(decisions):
            outcome_ru = {"satisfied": "Удовлетворено", "denied": "Отказано"}.get(
                d.outcome.value, "Не определено"
            )
            context = self._build_case_context(d)
            items.append(
                f"--- АКТ №{i + 1} ---\nИсход (автоматический): {outcome_ru}\nКонтекст:\n{context}"
            )

        cases_text = "\n\n".join(items)

        prompt = (
            f"Ты — элитный юрист-аналитик. Твоя задача — отобрать ТОЛЬКО релевантные судебные акты и предоставить безупречную аналитику.\n\n"
            f"ЗАПРОС ПОЛЬЗОВАТЕЛЯ: {query_text}\n"
            f"ЦЕЛЕВАЯ СТАТЬЯ: {article}\n\n"
            f"АКТЫ ДЛЯ АНАЛИЗА:\n"
            f"{cases_text}\n\n"
            f"СТРОГИЕ КРИТЕРИИ ДЛЯ 100% УВЕРЕННОСТИ:\n"
            f"1. **Суть**: Акт релевантен, если в нем документально доказано, что судебный акт отвечает запросу пользователя. Статья {article} должна быть ключевым элементом спора.\n"
            f"2. **Процессуальный мусор**: Обычное упоминание номера статьи в списке без связи с существом дела — relevant: false. Если есть малейшие сомнения — relevant: false.\n"
            f"3. **Контекст**: Если суть спора, указанная пользователем, на 100% совпадает с сутью акта, только тогда relevant: true.\n\n"
            f"ТРЕБОВАНИЯ К ПОЛЯМ ОТВЕТА:\n"
            f"- **reasons**: Пиши профессиональным юридическим языком суть спора.\n"
            f"- **proof_quote**: ТЫ ОБЯЗАН ДОКАЗАТЬ РЕЛЕВАНТНОСТЬ. Приведи точную цитату из текста ИЛИ твой строгий логический вывод (доказательство), который на 100% подтверждает релевантность запросу. БЕЗ ДОКАЗАТЕЛЬСТВА ДЕЛО СЧИТАЕТСЯ МУСОРОМ. Пустое поле разрешено только если relevant: false.\n"
            f'- **outcome**: "satisfied" (удовлетворено/обоснованно), "denied" (отказано/необоснованно) или "NOT FOUND IN DOCUMENT".\n\n'
            f"ФОРМАТ ОТВЕТА — СТРОГИЙ JSON-МАССИВ (ровно {len(decisions)} элементов):\n"
            f"[\n"
            f'  {{"relevant": true, "reasons": ["Обоснование"], "proof_quote": "Цитата", "outcome": "satisfied"}},\n'
            f'  {{"relevant": false, "reasons": ["Не подходит"], "proof_quote": "", "outcome": "NOT FOUND IN DOCUMENT"}}\n'
            f"]\n\n"
            f"НИКАКИХ пояснений. Твой ответ должен быть только валидным JSON-массивом."
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
                "max_tokens": 2500,  # Ensure full JSON response is received
            },
            timeout=self._timeout * 2,
        )
        response.raise_for_status()
        raw_content = response.json()["choices"][0]["message"]["content"]
        content = self._clean_llm_json(raw_content)

        try:
            raw = json.loads(content)
        except json.JSONDecodeError as e:
            self._logger.error(
                "llm.json_parse_failed",
                extra={"data": {"content": raw_content[:500], "error": str(e)}},
            )
            raise

        # Handle edge case: LLM returned a raw JSON string (double-encoded)
        if isinstance(raw, str):
            self._logger.warning(
                "llm.json_is_string", extra={"data": {"content": raw_content[:300]}}
            )
            try:
                raw = json.loads(raw)
            except Exception:
                raise ValueError(f"Expected list from LLM but got string: {raw[:100]}")

        # If LLM wrapped list in a dict (common quirk), try to extract it
        if isinstance(raw, dict):
            # Look for common keys: 'results', 'acts', 'data', or any list value
            for key in ["results", "acts", "decisions", "data", "items"]:
                if isinstance(raw.get(key), list):
                    raw = raw[key]
                    break
            else:
                # If no known key, use the first list value found
                for val in raw.values():
                    if isinstance(val, list):
                        raw = val
                        break

                    break

        if not isinstance(raw, list):
            self._logger.error(
                "llm.expected_list_failed", extra={"data": {"content": raw_content}}
            )
            raise ValueError("Expected list from LLM")

        results = []
        for item in raw:
            try:
                # Use Pydantic to enforce strict schema types and defaults
                parsed_item = ClassifyResultItem.model_validate(item)

                is_relevant = parsed_item.relevant
                reasons = parsed_item.reasons
                proof_quote = parsed_item.proof_quote.strip()

                # Extract LLM-determined outcome
                raw_outcome = parsed_item.outcome.strip().lower()
                llm_outcome: str | None = None
                if raw_outcome in ("satisfied", "удовлетворено"):
                    llm_outcome = "satisfied"
                elif raw_outcome in ("denied", "отказано"):
                    llm_outcome = "denied"
                # Else: NOT FOUND IN DOCUMENT implies None

                # Relaxed Logic: If relevant but absolutely no quote or rationale is provided, 
                # we no longer reject it. Instead, we use a fallback and flag it.
                if is_relevant and not proof_quote:
                    # Upgrade to a "Weak" match downstream but don't drop the case
                    proof_quote = "Прямая цитата не найдена в доступных фрагментах (LLM подтвердила релевантность без цитаты)"

                if not reasons:
                    reasons = [self._FALLBACK[0]]
                valid_reasons = tuple(
                    r for r in reasons if isinstance(r, str) and r.strip()
                )

                results.append((
                    is_relevant,
                    valid_reasons if valid_reasons else self._FALLBACK,
                    proof_quote,
                    llm_outcome,
                ))
            except ValidationError as e:
                self._logger.error(
                    "llm.pydantic_validation_failed",
                    extra={"data": {"item": str(item), "error": str(e)}},
                )
                results.append((False, self._FALLBACK, "", None))
            except Exception as e:
                self._logger.error(
                    "llm.item_process_failed",
                    extra={"data": {"item": str(item), "error": str(e)}},
                )
                results.append((False, self._FALLBACK, "", None))

        # Ensure we always return exactly one result per decision.
        # LLM may truncate output and return fewer JSON items than requested.
        if len(results) < len(decisions):
            missing = len(decisions) - len(results)
            self._logger.warning(
                "llm.batch_size_mismatch",
                extra={
                    "data": {
                        "expected": len(decisions),
                        "received": len(results),
                        "missing": missing,
                    }
                },
            )
            results.extend(
                [
                    (
                        True,
                        (self._FALLBACK[0], "LLM batch truncated"),
                        "",
                        None,
                    )
                ]
                * missing
            )

        return results[: len(decisions)]

    def _build_case_context(self, decision: CaseDecision) -> str | None:
        MAX_TEXT_CHARS = 5000  # Phase 2.5: Reduce context to save costs
        parts: list[str] = []
        if decision.case_number:
            parts.append(f"Дело: {decision.case_number}")
        if decision.court_name:
            parts.append(f"Суд: {decision.court_name}")
        if decision.case_category:
            cat_map = {"Б": "Банкротство", "Г": "Гражданское", "А": "Административное"}
            parts.append(
                f"Категория: {cat_map.get(decision.case_category, decision.case_category)}"
            )
        if decision.analysis_text:
            text = decision.analysis_text
            if len(text) > MAX_TEXT_CHARS:
                # Tail-priority: keep the LAST characters (where decisions usually are)
                text = "..." + text[-MAX_TEXT_CHARS:]
            parts.append(f"Текст события (последние важные данные): {text}")
        if decision.reasons and decision.reasons != ("оценка обстоятельств дела",):
            parts.append(f"Извлеченные основания: {', '.join(decision.reasons)}")
        return "\n".join(parts)

    async def _call_classify_with_retry(
        self,
        text: str,
        outcome: CaseOutcome,
        article: str,
        query_text: str = "",
    ) -> tuple[bool, tuple[str, ...], str]:
        last_exc: Exception = RuntimeError("no attempts")
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                return await self._call_classify_api(text, outcome, article, query_text)
            except httpx.HTTPStatusError as exc:
                if (
                    exc.response.status_code in {429, 503}
                    and attempt < self._MAX_RETRIES
                ):
                    await asyncio.sleep(1.0 * (attempt + 1))
                    last_exc = exc
                    continue
                raise
        raise last_exc

    async def _call_classify_api(
        self,
        context: str,
        outcome: CaseOutcome,
        article: str,
        query_text: str = "",
    ) -> tuple[bool, tuple[str, ...], str]:
        outcome_ru = {"satisfied": "Удовлетворено", "denied": "Отказано"}.get(
            outcome.value, "Не определено"
        )
        prompt = (
            f"Ты — элитный юрист-аналитик Арбитражных судов РФ. Твоя задача — ПРОВЕРИТЬ, относится ли этот акт прямо к запросу пользователя.\n\n"
            f"ЗАПРОС ПОЛЬЗОВАТЕЛЯ: {query_text}\n"
            f"ЦЕЛЕВАЯ СТАТЬЯ: {article}\n"
            f"КОНТЕКСТ ИЗ КАД:\n{context}\n"
            f"ИСХОД ДЕЛА: {outcome_ru}\n\n"
            f"СТРОГИЕ КРИТЕРИИ ДЛЯ 100% УВЕРЕННОСТИ:\n"
            f"1. СТАТЬЯ: Акт ДОЛЖЕН быть по ст. {article} в строгом контексте запроса ({query_text}). Если статья иная или контекст не совпадает — НЕ_РЕЛЕВАНТНО.\n"
            f"2. ЗАКОН: Акт должен относиться к той же области права, что и запрос. Если есть малейшие сомнения — НЕ_РЕЛЕВАНТНО.\n"
            f"3. СОДЕРЖАНИЕ: Процедурные определения БЕЗ сути рассматриваемого спора — НЕ_РЕЛЕВАНТНО.\n"
            f"4. СУТЬ И ДОКАЗАТЕЛЬСТВО: ТЫ ОБЯЗАН ДОКАЗАТЬ РЕЛЕВАНТНОСТЬ. В `proof_quote` приведи короткую цитату (1-2 предложения) или строгий логический вывод (доказательство), на 100% доказывающий суть спора по ст. {article}. БЕЗ ДОКАЗАТЕЛЬСТВА ДЕЛО СЧИТАЕТСЯ МУСОРОМ. Пустое поле разрешено только если relevant: false.\n\n"
            f"ВЕРНИ JSON:\n"
            f'{{ "relevant": true/false, "reasons": ["краткий тезис"], "proof_quote": "цитата" }}'
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
        raw_content = response.json()["choices"][0]["message"]["content"]
        content = self._clean_llm_json(raw_content)

        try:
            item = json.loads(content)
        except json.JSONDecodeError as e:
            self._logger.error(
                "llm.json_parse_failed",
                extra={"data": {"content": raw_content, "error": str(e)}},
            )
            raise

        if isinstance(item, list) and item:
            item = item[0]

        is_relevant = bool(item.get("relevant", True))
        reasons = item.get("reasons", [])
        proof_quote = item.get("proof_quote", "")

        if not isinstance(reasons, list) or not reasons:
            reasons = [self._FALLBACK[0]]

        # Relaxed Logic: If relevant but absolutely no quote or rationale is provided, 
        # we no longer reject it. Instead, we use a fallback and flag it.
        if is_relevant and not proof_quote:
            # Upgrade to a "Weak" match downstream but don't drop the case
            proof_quote = "Прямая цитата не найдена в доступных фрагментах (LLM подтвердила релевантность без цитаты)"

        valid_reasons = tuple(r for r in reasons if isinstance(r, str) and r.strip())

        return (
            is_relevant,
            valid_reasons if valid_reasons else self._FALLBACK,
            proof_quote,
        )

    async def _call_with_retry(
        self, text: str, outcome: CaseOutcome
    ) -> tuple[str, ...]:
        last_exc: Exception = RuntimeError("no attempts")
        for attempt in range(self._MAX_RETRIES + 1):
            try:
                return await self._call_api(text, outcome)
            except httpx.HTTPStatusError as exc:
                if (
                    exc.response.status_code in {429, 503}
                    and attempt < self._MAX_RETRIES
                ):
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
            f"Ты анализируешь судебный акт арбитражного суда.\n"
            f"Текст события: {text}\n"
            f"Результат: {outcome_ru}\n\n"
            f"Выбери 1–3 правовых основания, которые наиболее точно "
            f"соответствуют данному делу. Можешь использовать основания из списка ниже "
            f"или сформулировать свои, если дело не относится к перечисленным категориям. "
            f'Если информации недостаточно, верни ["оценка обстоятельств дела"].\n\n'
            f"Типовые основания:\n{labels_str}\n\n"
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
        # Accept any well-formed reason string — not just canonical labels.
        # This allows the LLM to return relevant reasons for any area of law.
        valid = tuple(r for r in raw if isinstance(r, str) and r.strip())
        return valid if valid else self._FALLBACK
