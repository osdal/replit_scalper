import asyncio
import logging
import sys

from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand

from config import settings
from storage.database import Database
from handlers.start import router as start_router
from handlers.connect import router as connect_router
from handlers.keys import router as keys_router
from handlers.support import router as support_router


async def main():
    logging.basicConfig(level=logging.INFO)
    if not settings.support_telegram_bot_token:
        logging.error("SUPPORT_BOT_TOKEN is not set")
        sys.exit(1)

    bot = Bot(
        token=settings.support_telegram_bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    db = Database(settings.support_bot_db)
    await db.init()

    dp.include_router(start_router)
    dp.include_router(connect_router)
    dp.include_router(keys_router)
    dp.include_router(support_router)

    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_my_commands([
        BotCommand(command="start", description="Запустить бота"),
    ])

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
