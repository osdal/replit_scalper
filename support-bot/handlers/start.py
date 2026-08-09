import logging
from aiogram import Router, F
from aiogram.types import Message
from aiogram.filters import CommandStart
from keyboards.main import main_menu, reply_keyboard

router = Router()
logger = logging.getLogger(__name__)


@router.message(CommandStart())
async def start_handler(message: Message):
    logger.info(f"START from chat_id={message.chat.id} user={message.from_user.id} username={message.from_user.username}")
    await message.answer(
        "Добро пожаловать!\nВыберите действие:",
        reply_markup=main_menu(),
    )
    await message.answer(
        "Или используйте кнопку ниже:",
        reply_markup=reply_keyboard(),
    )


@router.message(F.text == "🚀 Запустить бота")
async def menu_button_handler(message: Message):
    await message.answer(
        "Главное меню:",
        reply_markup=main_menu(),
    )
