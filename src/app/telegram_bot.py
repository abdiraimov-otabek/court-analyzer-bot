from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from src.app.analysis_notifications import send_slow_alert_if_needed
from src.app.bot_logging import (
    clear_request_context,
    configure_logging,
    log_event,
    new_request_id,
    set_request_context,
)
from src.app.config import load_config
from src.app.container import Container
from src.domain.value_objects import UserId
from src.services.case_exporter import build_cases_excel
from src.services.hashing import HashingService
from src.services.kad_client import (
    KadAccessError,
    KadInvalidResponseError,
    KadRateLimitError,
    KadUnavailableError,
)
from src.services.request_processor import (
    CourtNotFoundError,
    InsufficientQualityError,
    NotEnoughData,
)

config = load_config()
container = Container(config)
configure_logging("telegram-bot")
logger = logging.getLogger("telegram_bot")
hashing_service = HashingService(config.hash_salt) if config.hash_salt else None


dp = Dispatcher()


def _status_cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Статус", callback_data="cmd:status"),
                InlineKeyboardButton(text="Отменить", callback_data="cmd:cancel"),
            ]
        ]
    )


def _help_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Помощь", callback_data="cmd:help"),
            ]
        ]
    )


@dp.callback_query(lambda cb: cb.data and cb.data.startswith("cmd:"))
async def handle_command_button(callback) -> None:
    cmd = "/" + callback.data.split(":", 1)[1]
    if not callback.from_user:
        return
    user_id = UserId(str(callback.from_user.id))
    user_hash = hashing_service.hash_value(user_id.value) if hashing_service else "-"
    request_id = new_request_id()
    set_request_context(request_id, user_hash, "telegram_bot")
    bot_logic = container.build_bot_logic()
    now = callback.message.date.replace(tzinfo=None)
    responses = bot_logic.handle_message(user_id, cmd, now)
    await callback.answer()
    for response in responses:
        await callback.message.answer(response)
    clear_request_context()


@dp.message()
async def handle_message(message: Message) -> None:
    if message.text is None:
        return
    if not message.from_user:
        return
    user_id = UserId(str(message.from_user.id))
    user_hash = hashing_service.hash_value(user_id.value) if hashing_service else "-"
    request_id = new_request_id()
    set_request_context(request_id, user_hash, "telegram_bot")
    log_event(
        logger,
        "message.received",
        text=message.text,
        text_length=len(message.text),
    )
    bot_logic = container.build_bot_logic()
    now = message.date.replace(tzinfo=None)

    # Commands are instant — handle synchronously and return.
    if message.text in {"/start", "/help", "/status", "/cancel"}:
        responses = bot_logic.handle_message(user_id, message.text, now)
        for response in responses:
            await message.answer(response)
        clear_request_context()
        return

    # Non-command: validate without HTTP, then count asynchronously.
    pre_result = bot_logic.pre_validate(user_id, message.text, now)
    if isinstance(pre_result, list):
        for response in pre_result:
            await message.answer(response)
        clear_request_context()
        return

    # Ack the user immediately — before any network call.
    await message.answer(
        "Принял. Оцениваю объем данных...", reply_markup=_status_cancel_kb()
    )
    asyncio.create_task(
        _count_and_analyze(
            message, user_id, pre_result.query_text, pre_result.is_quarter_selection
        )
    )
    clear_request_context()


async def _count_and_analyze(
    message: Message,
    user_id: UserId,
    query_text: str,
    is_quarter_selection: bool,
) -> None:
    """Background task: count cases asynchronously, then send results and optionally analyse."""
    request_id = new_request_id()
    user_hash = hashing_service.hash_value(user_id.value) if hashing_service else "-"
    set_request_context(request_id, user_hash, "telegram_bot")
    try:
        kad_client = container._get_kad_client()
        settings = container.settings_service.get_settings()
        count = await kad_client.count_cases_async(query_text, settings)
        log_event(logger, "count_cases.completed", count=count)
    except KadRateLimitError:
        log_event(logger, "count_cases.failed", error_type="KadRateLimitError")
        await message.answer(
            "КАД временно ограничил доступ. Попробуйте позже.", reply_markup=_help_kb()
        )
        container.active_requests.finish(user_id)
        clear_request_context()
        return
    except KadUnavailableError:
        log_event(logger, "count_cases.failed", error_type="KadUnavailableError")
        await message.answer(
            "КАД временно недоступен. Попробуйте позже.", reply_markup=_help_kb()
        )
        container.active_requests.finish(user_id)
        clear_request_context()
        return
    except KadInvalidResponseError:
        log_event(logger, "count_cases.failed", error_type="KadInvalidResponseError")
        await message.answer(
            "КАД вернул некорректный ответ. Запрос прерван.", reply_markup=_help_kb()
        )
        container.active_requests.finish(user_id)
        clear_request_context()
        return
    except KadAccessError:
        log_event(logger, "count_cases.failed", error_type="KadAccessError")
        await message.answer(
            "Доступ к КАД ограничен. Проверьте ключ и лимиты.", reply_markup=_help_kb()
        )
        container.active_requests.finish(user_id)
        clear_request_context()
        return
    except Exception:
        logger.exception("count_cases.failed_unexpected")
        await message.answer(
            "Не удалось обработать запрос. Попробуйте позже.", reply_markup=_help_kb()
        )
        container.active_requests.finish(user_id)
        clear_request_context()
        return

    bot_logic = container.build_bot_logic()
    messages, should_analyze = bot_logic.apply_count_result(
        user_id, query_text, count, is_quarter_selection
    )
    for msg in messages:
        kb = _status_cancel_kb() if should_analyze else None
        await message.answer(msg, reply_markup=kb)

    clear_request_context()
    if should_analyze:
        await _run_analysis(message, user_id, query_text)


async def _run_analysis(message: Message, user_id: UserId, query_text: str) -> None:
    request_id = new_request_id()
    user_hash = hashing_service.hash_value(user_id.value) if hashing_service else "-"
    set_request_context(request_id, user_hash, "telegram_bot")
    settings = container.settings_service.get_settings()
    processor = container.build_request_processor()
    slow_alert_task = asyncio.create_task(
        send_slow_alert_if_needed(
            send_answer=message.answer,
            delay_seconds=settings.slow_alert_minutes * 60,
        )
    )
    try:
        result = await processor.process(user_id, query_text, settings)
        slow_alert_task.cancel()
        await message.answer(result.summary, reply_markup=_help_kb())
        excel_bytes = build_cases_excel(result.case_list)
        file = BufferedInputFile(excel_bytes, filename="cases.xlsx")
        await message.answer_document(file)
        log_event(
            logger, "analysis.completed", total_cases=len(result.case_list.splitlines())
        )
    except KadRateLimitError:
        log_event(logger, "analysis.failed", error_type="KadRateLimitError")
        await message.answer(
            "КАД временно ограничил доступ. Попробуйте позже.", reply_markup=_help_kb()
        )
    except KadUnavailableError:
        log_event(logger, "analysis.failed", error_type="KadUnavailableError")
        await message.answer(
            "КАД временно недоступен. Попробуйте позже.", reply_markup=_help_kb()
        )
    except KadAccessError:
        log_event(logger, "analysis.failed", error_type="KadAccessError")
        await message.answer(
            "Доступ к КАД ограничен. Проверьте ключ и лимиты.", reply_markup=_help_kb()
        )
    except KadInvalidResponseError:
        log_event(logger, "analysis.failed", error_type="KadInvalidResponseError")
        await message.answer(
            "КАД вернул некорректный ответ. Запрос прерван.", reply_markup=_help_kb()
        )
    except InsufficientQualityError as exc:
        log_event(
            logger,
            "analysis.failed_quality",
            reason_code=exc.reason_code,
            total_cases=exc.total_cases,
            known_cases=exc.known_cases,
            unknown_cases=exc.unknown_cases,
            unknown_share=round(exc.unknown_share, 4),
            court_mismatch_share=round(exc.court_mismatch_share, 4),
        )
        await message.answer(exc.summary, reply_markup=_help_kb())
        if settings.send_partial_file_on_quality_fail and exc.case_list.strip():
            excel_bytes = build_cases_excel(exc.case_list)
            file = BufferedInputFile(excel_bytes, filename="cases.xlsx")
            await message.answer_document(file)
    except CourtNotFoundError:
        log_event(logger, "analysis.failed", error_type="CourtNotFoundError")
        await message.answer(
            "По вашему запросу не найдено дел в указанном суде. "
            "Возможно, название суда указано неточно или суд не поддерживается системой КАД. "
            "Уточните суд и попробуйте снова.",
            reply_markup=_help_kb(),
        )
    except NotEnoughData:
        log_event(logger, "analysis.failed", error_type="NotEnoughData")
        await message.answer(
            "Найдено менее 5 дел. Недостаточно данных для статистического анализа. "
            "Уточните запрос или период.",
            reply_markup=_help_kb(),
        )
    except Exception:
        logger.exception("analysis.failed_unexpected")
        await message.answer(
            "Не удалось завершить анализ. Попробуйте позже.", reply_markup=_help_kb()
        )
    finally:
        slow_alert_task.cancel()
        container.active_requests.finish(user_id)
        clear_request_context()


def build_bot() -> Bot:
    if not config.telegram_bot_token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    return Bot(token=config.telegram_bot_token)


async def close_container() -> None:
    await container.aclose()
