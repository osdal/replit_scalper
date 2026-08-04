from aiogram import Router
from aiogram.types import Message
from aiogram.filters import CommandStart
from keyboards.main import main_menu


router = Router()


@router.message(CommandStart())
async def start_handler(message: Message):
    await message.answer(
        "Добро пожаловать!\nВыберите действие:",
        reply_markup=main_menu(),
    )
