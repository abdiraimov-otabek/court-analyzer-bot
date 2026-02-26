from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from src.app.bot_logging import log_event
from src.domain.settings import Settings
from src.domain.value_objects import QueryText, UserId
from src.services.access_control import AccessControlList
from src.services.active_requests import ActiveRequestRegistry
from src.services.quarter_selection import (
    PendingQuarterSelection,
    QuarterSelectionRegistry,
)
from src.services.query_validator import QueryValidationResult, QueryValidator
from src.services.rate_limit import HourlyRateLimiter


@dataclass
class PreValidateResult:
    """Returned by BotLogic.pre_validate when a query passes all sync checks."""

    query_text: str
    is_quarter_selection: bool


class CountProvider(Protocol):
    def count_cases(self, query_text: str, settings: Settings) -> int: ...


class SettingsProvider(Protocol):
    def get_settings(self) -> Settings: ...


class BotLogic:
    def __init__(
        self,
        access_control: AccessControlList,
        rate_limiter: HourlyRateLimiter,
        active_requests: ActiveRequestRegistry,
        quarter_selections: QuarterSelectionRegistry,
        count_provider: CountProvider,
        settings_provider: SettingsProvider,
        estimate_minutes,
        query_validator: QueryValidator | None = None,
    ) -> None:
        self._access_control = access_control
        self._rate_limiter = rate_limiter
        self._active_requests = active_requests
        self._quarter_selections = quarter_selections
        self._count_provider = count_provider
        self._settings_provider = settings_provider
        self._estimate_minutes = estimate_minutes
        self._query_validator = query_validator or QueryValidator()
        self._logger = logging.getLogger("bot_logic")

    def handle_message(self, user_id: UserId, text: str, now: datetime) -> list[str]:
        if text == "/start":
            return [
                "Привет! Я помогу проанализировать судебные акты из КАД. "
                "Укажите в запросе суд и период. "
                "Пример: 'Практика по статье 61.2 Закона о банкротстве в АС Москвы за 2024 год'."
            ]
        if text == "/help":
            return [
                "Отправьте запрос на естественном языке. Доступны команды: /status, /cancel. "
                "Обязательно укажите суд и период (год или квартал+год). "
                "Пример: 'Практика по статье 61.2 Закона о банкротстве в АС Москвы за 2024 год'."
            ]
        if text == "/status":
            active = self._active_requests.get(user_id)
            if active is None:
                if self._quarter_selections.get_pending(user_id) is not None:
                    return ["Ожидаю выбор квартала: 1, 2, 3 или 4."]
                return ["Нет активного запроса."]
            if active.cancelled:
                return ["Запрос отменяется."]
            if active.phase == "counting":
                return ["Оцениваю объем данных..."]
            if active.phase == "collecting":
                return [
                    f"подбираю дела для анализа, найдено {active.collected_cases} из {active.total_cases} дел."
                ]
            if active.successful_cases and active.successful_cases != active.processed_cases:
                return [
                    f"анализирую, обработано {active.processed_cases} из {active.total_cases} дел "
                    f"(успешно загружено {active.successful_cases})."
                ]
            return [
                f"анализирую, обработано {active.processed_cases} из {active.total_cases} дел."
            ]
        if text == "/cancel":
            pending = self._quarter_selections.get_pending(user_id)
            self._quarter_selections.clear_pending(user_id)
            active = self._active_requests.get(user_id)
            if pending is not None and active is None:
                return ["Запрос отменен."]
            if active is None:
                return ["Нет активного запроса."]
            self._active_requests.cancel(user_id)
            return ["Запрос отменен."]

        settings = self._settings_provider.get_settings()
        if not settings.allow_all_users and not self._access_control.is_allowed(
            user_id
        ):
            return ["Доступ ограничен. Обратитесь к администратору."]

        pending_quarter = self._quarter_selections.get_pending(user_id)
        if pending_quarter is not None:
            return self._handle_quarter_selection(user_id, text, pending_quarter)

        if self._active_requests.get(user_id) is not None:
            return ["У вас уже есть активный запрос. Дождитесь его завершения."]

        if not self._rate_limiter.allow(user_id, now):
            return ["Превышен лимит 10 запросов в час. Попробуйте позже."]

        return self._start_query(
            user_id=user_id, raw_query=text, include_ack=True, apply_rate_limit=False
        )

    def pre_validate(
        self, user_id: UserId, text: str, now: datetime
    ) -> PreValidateResult | list[str]:
        """Validate a non-command query with no HTTP calls.

        Starts the active request (phase="counting") on success so the caller
        can immediately acknowledge the user and then count asynchronously.

        Returns PreValidateResult on success or a list of error strings on failure.
        """
        settings = self._settings_provider.get_settings()
        if not settings.allow_all_users and not self._access_control.is_allowed(
            user_id
        ):
            return ["Доступ ограничен. Обратитесь к администратору."]

        pending_quarter = self._quarter_selections.get_pending(user_id)
        if pending_quarter is not None:
            choice = text.strip()
            if choice not in {"1", "2", "3", "4"}:
                return ["Выберите квартал цифрой: 1, 2, 3 или 4."]
            self._quarter_selections.clear_pending(user_id)
            quarter_query = self._build_quarter_query(
                pending_quarter.base_query, choice
            )
            query = QueryText(quarter_query)
            started = self._active_requests.start(
                user_id, query_text=query.value, total_cases=0, phase="counting"
            )
            if not started:
                return ["У вас уже есть активный запрос. Дождитесь его завершения."]
            return PreValidateResult(query_text=query.value, is_quarter_selection=True)

        if self._active_requests.get(user_id) is not None:
            return ["У вас уже есть активный запрос. Дождитесь его завершения."]

        if not self._rate_limiter.allow(user_id, now):
            return ["Превышен лимит 10 запросов в час. Попробуйте позже."]

        validation = self._query_validator.validate(text)
        if not validation.is_valid:
            return [self._build_validation_message(validation)]

        query = QueryText(text)
        started = self._active_requests.start(
            user_id, query_text=query.value, total_cases=0, phase="counting"
        )
        if not started:
            return ["У вас уже есть активный запрос. Дождитесь его завершения."]
        return PreValidateResult(query_text=query.value, is_quarter_selection=False)

    def apply_count_result(
        self,
        user_id: UserId,
        query_text: str,
        count: int,
        is_quarter_selection: bool,
    ) -> tuple[list[str], bool]:
        """Process the result of an async count.

        Returns (messages_to_send, should_run_analysis).
        When should_run_analysis is True the caller must invoke the analysis pipeline.
        When False the active request has already been finished.
        """
        settings = self._settings_provider.get_settings()

        if self._active_requests.is_cancelled(user_id):
            self._active_requests.finish(user_id)
            return ["Запрос отменен."], False

        if count == 0:
            self._active_requests.finish(user_id)
            return [
                "По вашему запросу в КАД не найдено подходящих судебных актов. "
                "Попробуйте изменить формулировку."
            ], False

        if count > settings.max_cases:
            if is_quarter_selection:
                self._active_requests.update_total_cases(user_id, settings.max_cases)
                self._active_requests.update_query_text(user_id, query_text)
                self._active_requests.set_phase(user_id, "analyzing")
                estimate = self._estimate_minutes(settings.max_cases)
                return [
                    f"В выбранном периоде найдено более {settings.max_cases} дел. "
                    f"Для соблюдения лимита анализирую до {settings.max_cases} дел.\n"
                    f"Начинаю анализ до {settings.max_cases} дел. Это займет ~{estimate} минут."
                ], True
            self._active_requests.finish(user_id)
            self._quarter_selections.set_pending(user_id, query_text)
            return [self._quarter_options_message(settings.max_cases)], False

        self._active_requests.update_total_cases(user_id, count)
        self._active_requests.update_query_text(user_id, query_text)
        self._active_requests.set_phase(user_id, "analyzing")
        estimate = self._estimate_minutes(count)
        return [f"Начинаю анализ ~{count} дел. Это займет ~{estimate} минут."], True

    def _handle_quarter_selection(
        self,
        user_id: UserId,
        text: str,
        pending_quarter: PendingQuarterSelection,
    ) -> list[str]:
        choice = text.strip()
        if choice not in {"1", "2", "3", "4"}:
            return ["Выберите квартал цифрой: 1, 2, 3 или 4."]
        self._quarter_selections.clear_pending(user_id)
        quarter_query = self._build_quarter_query(pending_quarter.base_query, choice)
        return self._start_query(
            user_id=user_id,
            raw_query=quarter_query,
            include_ack=False,
            apply_rate_limit=False,
            allow_cap_on_overflow=True,
        )

    def _start_query(
        self,
        user_id: UserId,
        raw_query: str,
        include_ack: bool,
        apply_rate_limit: bool,
        allow_cap_on_overflow: bool = False,
    ) -> list[str]:
        if apply_rate_limit and not self._rate_limiter.allow(user_id, datetime.now()):
            return ["Превышен лимит 10 запросов в час. Попробуйте позже."]

        validation = self._query_validator.validate(raw_query)
        if not validation.is_valid:
            return [self._build_validation_message(validation)]

        query = QueryText(raw_query)
        started = self._active_requests.start(
            user_id, query_text=query.value, total_cases=0, phase="counting"
        )
        if not started:
            return ["У вас уже есть активный запрос. Дождитесь его завершения."]
        ack = "Принял. Оцениваю объем данных..."
        try:
            settings = self._settings_provider.get_settings()
            count_start = datetime.now()
            count = self._count_provider.count_cases(query.value, settings)
            count_duration_ms = int(
                (datetime.now() - count_start).total_seconds() * 1000
            )
            log_event(
                self._logger,
                "count_cases.completed",
                count=count,
                duration_ms=count_duration_ms,
            )
        except Exception:
            self._active_requests.finish(user_id)
            raise
        if self._active_requests.is_cancelled(user_id):
            self._active_requests.finish(user_id)
            return ["Запрос отменен."]
        if count == 0:
            self._active_requests.finish(user_id)
            messages = [
                "По вашему запросу в КАД не найдено подходящих судебных актов. "
                "Попробуйте изменить формулировку."
            ]
            if include_ack:
                messages.insert(0, ack)
            return messages
        if count > settings.max_cases:
            if allow_cap_on_overflow:
                self._active_requests.update_total_cases(user_id, settings.max_cases)
                self._active_requests.update_query_text(user_id, query.value)
                self._active_requests.set_phase(user_id, "analyzing")
                estimate = self._estimate_minutes(settings.max_cases)
                return [
                    (
                        f"В выбранном периоде найдено более {settings.max_cases} дел. "
                        f"Для соблюдения лимита анализирую до {settings.max_cases} дел.\n"
                        f"Начинаю анализ до {settings.max_cases} дел. Это займет ~{estimate} минут."
                    )
                ]
            self._active_requests.finish(user_id)
            self._quarter_selections.set_pending(user_id, query.value)
            messages = [self._quarter_options_message(settings.max_cases)]
            if include_ack:
                messages.insert(0, ack)
            return messages

        self._active_requests.update_total_cases(user_id, count)
        self._active_requests.update_query_text(user_id, query.value)
        self._active_requests.set_phase(user_id, "analyzing")
        estimate = self._estimate_minutes(count)
        messages = [f"Начинаю анализ ~{count} дел. Это займет ~{estimate} минут."]
        if include_ack:
            messages.insert(0, ack)
        return messages

    def _quarter_options_message(self, max_cases: int) -> str:
        return (
            f"По вашему запросу найдено более {max_cases} дел, анализ займет слишком много времени. "
            "Для быстрого результата могу проанализировать данные за один квартал. Выберите период:\n"
            "1. Январь-март,\n"
            "2. Апрель-июнь,\n"
            "3. Июль-сентябрь,\n"
            "4. Октябрь-декабрь."
        )

    def _build_quarter_query(self, base_query: str, choice: str) -> str:
        quarter_map = {
            "1": "1 квартал",
            "2": "2 квартал",
            "3": "3 квартал",
            "4": "4 квартал",
        }
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", base_query)
        year = year_match.group(1) if year_match else None
        quarter_label = quarter_map[choice]
        if year:
            return f"{base_query} {quarter_label} {year}"
        return f"{base_query} {quarter_label}"

    def _build_validation_message(self, validation: QueryValidationResult) -> str:
        if validation.missing_court and validation.missing_period:
            return (
                "Для запуска анализа укажите суд и период (год или квартал+год). "
                "Пример: 'Практика по статье 61.2 Закона о банкротстве в АС Москвы за 2024 год'."
            )
        if validation.missing_court:
            return (
                "Уточните суд для анализа. "
                "Пример: '... в АС Москвы за 2024 год' или '... в 9 ААС за 2023 год'."
            )
        return (
            "Уточните период анализа (год или квартал+год). "
            "Пример: '... за 2024 год' или '... 2 квартал 2024'."
        )
