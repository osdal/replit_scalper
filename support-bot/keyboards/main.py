from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton


def main_menu(connected: bool = False) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(text="Подключить бота", callback_data="connect_start")],
        [InlineKeyboardButton(text="Мои ключи", callback_data="keys_list")],
        [InlineKeyboardButton(text="Поддержка", url="https://t.me/osdal?text=Здравствуйте!%20Я%20по%20поводу%20подключения%20бота%20для%20торговли%20на%20Binance")],
    ]
    if connected:
        buttons[0] = [InlineKeyboardButton(text="Заменить ключи", callback_data="connect_replace")]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def confirm_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data="connect_confirm")],
        [InlineKeyboardButton(text="Отмена", callback_data="connect_cancel")],
    ])


def back_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="main_menu")],
    ])
