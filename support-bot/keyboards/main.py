from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подключить бота", callback_data="connect_start")],
        [InlineKeyboardButton(text="Мои ключи", callback_data="keys_list")],
        [InlineKeyboardButton(text="Поддержка", url="https://t.me/osdal?text=Здравствуйте!%20Я%20по%20поводу%20подключения%20бота%20для%20торговли%20на%20Binance")],
    ])


def mode_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Только чтение", callback_data="mode:readonly")],
        [InlineKeyboardButton(text="Торговля", callback_data="mode:trade")],
    ])


def confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data="connect_confirm")],
        [InlineKeyboardButton(text="Отмена", callback_data="connect_cancel")],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")],
    ])
