from __future__ import annotations

import asyncio
import os


def _port() -> int:
    return int(os.getenv("PORT", "8000"))


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


async def _run_bot() -> None:
    from src.app.run_bot import main as run_bot_main

    await run_bot_main()


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
