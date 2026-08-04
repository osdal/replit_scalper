from aiogram import Router, F
from aiogram.types import Message, CallbackQuery
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from binance_connector import Client
from cryptography.fernet import Fernet

from config import settings
from crypto import encrypt_text
from storage.database import Database
from keyboards.main import mode_menu, confirm_menu, back_menu

router = Router()
db = Database(settings.database_path)


class ConnectStates(StatesGroup):
    waiting_api_key = State()
    waiting_api_secret = State()
    waiting_symbol = State()
    waiting_mode = State()
    waiting_confirm = State()


@router.callback_query(F.data == "connect_start")
async def connect_start(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Введите API-ключ Binance:", reply_markup=back_menu())
    await state.set_state(ConnectStates.waiting_api_key)


@router.message(ConnectStates.waiting_api_key)
async def connect_api_key(message: Message, state: FSMContext):
    await state.update_data(api_key=message.text.strip())
    await message.answer("Введите API-secret Binance:", reply_markup=back_menu())
    await state.set_state(ConnectStates.waiting_api_secret)


@router.message(ConnectStates.waiting_api_secret)
async def connect_api_secret(message: Message, state: FSMContext):
    await state.update_data(api_secret=message.text.strip())
    await message.answer("Введите тикер символа (например BTCUSDT):", reply_markup=back_menu())
    await state.set_state(ConnectStates.waiting_symbol)


@router.message(ConnectStates.waiting_symbol)
async def connect_symbol(message: Message, state: FSMContext):
    symbol = message.text.strip().upper()
    await state.update_data(symbol=symbol)
    await message.answer("Выберите режим доступа:", reply_markup=mode_menu())
    await state.set_state(ConnectStates.waiting_mode)


@router.callback_query(ConnectStates.waiting_mode, F.data.startswith("mode:"))
async def connect_mode(callback: CallbackQuery, state: FSMContext):
    mode = callback.data.split(":")[1]
    await state.update_data(mode=mode)
    data = await state.get_data()
    api_key = data.get("api_key", "")
    api_secret = data.get("api_secret", "")
    symbol = data.get("symbol", "")
    await callback.message.edit_text(
        f"Подтвердите подключение:\n\n"
        f"Символ: {symbol}\n"
        f"Режим: {'Только чтение' if mode == 'readonly' else 'Торговля'}\n\n"
        f"API-ключ: {api_key[:6]}...{api_key[-4:]}",
        reply_markup=confirm_menu(),
    )
    await state.set_state(ConnectStates.waiting_confirm)


@router.callback_query(ConnectStates.waiting_confirm, F.data == "connect_confirm")
async def connect_confirm(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    api_key = data.get("api_key", "")
    api_secret = data.get("api_secret", "")
    symbol = data.get("symbol", "")
    mode = data.get("mode", "trade")

    try:
        client = Client(api_key, api_secret, base_url="https://fapi.binance.com")
        account = client.account()
        await callback.message.edit_text("Ключ валиден. Сохраняем...")
    except Exception:
        await callback.message.edit_text("Ошибка валидации ключа. Попробуйте позже.", reply_markup=back_menu())
        await state.clear()
        return

    encrypted = encrypt_text(settings.master_key, api_secret)
    user = await db.get_or_create_user(
        telegram_id=callback.from_user.id,
        username=callback.from_user.username,
        first_name=callback.from_user.first_name,
        last_name=callback.from_user.last_name,
    )
    await db.add_api_key(
        user_id=user.id,
        symbol=symbol,
        mode=mode,
        encrypted_key=api_key,
        encrypted_secret=encrypted["encrypted"],
        iv=encrypted["iv"],
    )
    await callback.message.edit_text("Ключ сохранён. Бот готов к работе.", reply_markup=main_menu())
    await state.clear()


@router.callback_query(ConnectStates.waiting_confirm, F.data == "connect_cancel")
async def connect_cancel(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text("Подключение отменено.", reply_markup=main_menu())
    await state.clear()


@router.callback_query(F.data == "main_menu")
async def back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text("Главное меню:", reply_markup=main_menu())
