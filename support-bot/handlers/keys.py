from aiogram import Router
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext

from storage.database import Database
from keyboards.main import back_menu
from config import settings

router = Router()
db = Database(settings.database_path)


@router.callback_query(F.data == "keys_list")
async def keys_list(callback: CallbackQuery):
    keys = await db.get_user_keys(callback.from_user.id)
    if not keys:
        await callback.message.edit_text("У вас нет подключённых ключей.", reply_markup=back_menu())
        return
    text = "Ваши ключи:\n\n"
    for k in keys:
        status = "✅ Активен" if k.is_active else "❌ Отключен"
        text += f"• {k.symbol} | {k.mode} | {status}\n"
    await callback.message.edit_text(text, reply_markup=back_menu())


@router.message(Command("keys"))
async def keys_command(message: Message):
    await keys_list_by_message(message)


async def keys_list_by_message(message: Message):
    keys = await db.get_user_keys(message.from_user.id)
    if not keys:
        await message.answer("У вас нет подключённых ключей.")
        return
    text = "Ваши ключи:\n\n"
    for k in keys:
        status = "✅ Активен" if k.is_active else "❌ Отключен"
        text += f"• {k.symbol} | {k.mode} | {status}\n"
    await message.answer(text)
