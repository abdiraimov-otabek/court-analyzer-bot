import asyncio

from aiogram.exceptions import TelegramUnauthorizedError
from src.app.telegram_bot import build_bot, close_container, dp


async def main() -> None:
    try:
        bot = build_bot()
        await dp.start_polling(bot)
    except TelegramUnauthorizedError:
        print("\n[ERROR] Telegram server says: Unauthorized.")
        print("This usually means the TELEGRAM_BOT_TOKEN in your .env file is missing or invalid.")
        print("Please check @BotFather to get a valid token and update your .env file.\n")
    except Exception as e:
        print(f"\n[ERROR] Unexpected error: {e}\n")
    finally:
        await close_container()


if __name__ == "__main__":
    asyncio.run(main())
