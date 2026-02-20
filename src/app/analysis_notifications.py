from __future__ import annotations

import asyncio
from typing import Awaitable, Callable


async def send_slow_alert_if_needed(
    send_answer: Callable[[str], Awaitable[None]],
    delay_seconds: int,
) -> None:
    try:
        await asyncio.sleep(delay_seconds)
        await send_answer(
            "Анализ все еще выполняется из-за нагрузки или ограничений API. "
            "Продолжаю обработку и отправлю результат после завершения."
        )
    except asyncio.CancelledError:
        return
