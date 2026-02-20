import asyncio

from src.app.telegram_bot import build_bot, close_container, dp


async def main() -> None:
    bot = build_bot()
    try:
        await dp.start_polling(bot)
    finally:
        await close_container()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
