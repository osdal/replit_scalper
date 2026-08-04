from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from binance.spot import Spot

from config import settings
from crypto import encrypt_text
from storage.database import Database
from keyboards.main import confirm_menu, back_menu, main_menu

router = Router()
db = Database(settings.support_bot_db)


class ConnectStates(StatesGroup):
    waiting_api_key = State()
    waiting_api_secret = State()
    waiting_confirm = State()


@router.callback_query(F.data == "connect_start")
async def connect_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите API-ключ Binance:", reply_markup=back_menu())
    await state.set_state(ConnectStates.waiting_api_key)


@router.callback_query(F.data == "connect_replace")
async def connect_replace(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите новый API-ключ Binance:", reply_markup=back_menu())
    await state.set_state(ConnectStates.waiting_api_key)


@router.message(ConnectStates.waiting_api_key)
async def connect_api_key(message: Message, state: FSMContext):
    await state.update_data(api_key=message.text.strip())
    await message.answer("Введите API-secret Binance:", reply_markup=back_menu())
    await state.set_state(ConnectStates.waiting_api_secret)


@router.message(ConnectStates.waiting_api_secret)
async def connect_api_secret(message: Message, state: FSMContext):
    await state.update_data(api_secret=message.text.strip())
    data = await state.get_data()
    api_key = data.get("api_key", "")
    api_secret = data.get("api_secret", "")
    masked = f"{api_key[:6]}...{api_key[-4:]}" if len(api_key) > 10 else api_key
    await message.answer(
        f"Подтвердите подключение:\n\nAPI-ключ: {masked}",
        reply_markup=confirm_menu(),
    )
    await state.set_state(ConnectStates.waiting_confirm)


@router.callback_query(ConnectStates.waiting_confirm, F.data == "connect_confirm")
async def connect_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    api_key = data.get("api_key", "")
    api_secret = data.get("api_secret", "")

    try:
        client = Spot(api_key, api_secret)
        client.account()
        await callback.message.edit_text("Ключ валиден. Сохраняем...")
    except Exception as e:
        await callback.message.edit_text(
            f"Ошибка валидации ключа: {e}\nПопробуйте ещё раз.",
            reply_markup=back_menu(),
        )
        await state.clear()
        return

    user = await db.get_or_create_user(
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
    )
    encrypted = encrypt_text(settings.master_key, api_secret)
    await db.upsert_credentials(
        user_id=user.id,
        encrypted_api_key=api_key,
        encrypted_api_secret=encrypted["encrypted"],
        iv=encrypted["iv"],
    )
    await callback.message.edit_text("Ключ сохранён. Бот готов к работе.", reply_markup=main_menu(connected=True))
    await state.clear()


@router.callback_query(ConnectStates.waiting_confirm, F.data == "connect_cancel")
async def connect_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Подключение отменено.", reply_markup=main_menu())
    await state.clear()


@router.callback_query(F.data == "main_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    existing = await db.get_credentials(user_id)
    await state.clear()
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu(connected=bool(existing and existing.is_active)))
