from __future__ import annotations

import asyncio
import logging
import os


logger = logging.getLogger("run_service")


def _port() -> int:
    return int(os.getenv("PORT", "8000"))


def _bot_restart_delay_seconds() -> float:
    raw = os.getenv("BOT_RESTART_DELAY_SECONDS", "5")
    try:
        value = float(raw)
    except ValueError:
        return 5.0
    return max(0.0, value)


async def _run_admin_server() -> None:
    import uvicorn

    config = uvicorn.Config(
        "src.app.admin_api:app",
        host="0.0.0.0",
        port=_port(),
        reload=False,
    )
    server = uvicorn.Server(config)
    await server.serve()


def _is_transient_bot_error(exc: BaseException) -> bool:
    if isinstance(exc, TimeoutError):
        return True

    module = type(exc).__module__
    name = type(exc).__name__
    if module.startswith("aiogram") and name == "TelegramNetworkError":
        return True

    return False


async def _run_bot() -> None:
    from src.app.run_bot import main as run_bot_main

    while True:
        try:
            await run_bot_main()
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not _is_transient_bot_error(exc):
                raise
            logger.warning(
                "bot.polling.transient_failure",
                extra={"error_type": type(exc).__name__, "error": str(exc)},
            )
            await asyncio.sleep(_bot_restart_delay_seconds())


async def _run_combined() -> None:
    tasks = {
        asyncio.create_task(_run_admin_server()),
        asyncio.create_task(_run_bot()),
    }
    done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

    for task in pending:
        task.cancel()

    for task in pending:
        try:
            await task
        except asyncio.CancelledError:
            pass

    for task in done:
        task.result()


def main() -> None:
    role = os.getenv("APP_ROLE", "bot").strip().lower()

    if role == "admin":
        asyncio.run(_run_admin_server())
        return

    if role == "bot":
        asyncio.run(_run_bot())
        return

    if role == "combined":
        asyncio.run(_run_combined())
        return

    raise RuntimeError(
        "Unsupported APP_ROLE. Expected 'bot', 'admin', or 'combined'."
    )


if __name__ == "__main__":
    main()
