from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext

from storage.database import Database
from keyboards.main import back_menu, main_menu, reply_keyboard
from config import settings

router = Router()
db = Database(settings.support_bot_db)


@router.callback_query(F.data == "keys_list")
async def keys_list(callback: CallbackQuery):
    creds = await db.get_credentials(callback.from_user.id)
    if not creds or not creds.is_active:
        await callback.message.edit_text("У вас нет подключённых ключей.", reply_markup=main_menu(connected=False))
        await callback.message.answer("Меню:", reply_markup=reply_keyboard())
        return
    masked = f"{creds.encrypted_api_key[:6]}...{creds.encrypted_api_key[-4:]}"
    await callback.message.edit_text(
        f"Ключи подключены:\n\nAPI-key: {masked}\nСтатус: ✅ Активен",
        reply_markup=main_menu(connected=True),
    )
    await callback.message.answer("Меню:", reply_markup=reply_keyboard())


@router.message(Command("keys"))
async def keys_command(message: Message):
    creds = await db.get_credentials(message.from_user.id)
    if not creds or not creds.is_active:
        await message.answer("У вас нет подключённых ключей.")
        return
    masked = f"{creds.encrypted_api_key[:6]}...{creds.encrypted_api_key[-4:]}"
    await message.answer(f"Ключи подключены:\n\nAPI-key: {masked}\nСтатус: ✅ Активен")