import logging
from aiogram import Router
from aiogram.types import Message
from aiogram.filters import CommandStart
from keyboards.main import main_menu

router = Router()
logger = logging.getLogger(__name__)


@router.message(CommandStart())
async def start_handler(message: Message):
    logger.info(f"START from chat_id={message.chat.id} user={message.from_user.id} username={message.from_user.username}")
    await message.answer(
        "Добро пожаловать!\nВыберите действие:",
        reply_markup=main_menu(),
    )
