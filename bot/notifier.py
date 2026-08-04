
import os
import asyncio
import io
import logging

from telegram import Bot, InlineKeyboardMarkup, InlineKeyboardButton
from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ── Настройки рендера картинок ────────────────────────────────────────────
BG_COLOR = (24, 26, 33)          # тёмно-сине-серый фон
PANEL_COLOR = (34, 38, 48)       # панель-подложка
TEXT_COLOR = (235, 238, 245)     # светлый основной текст
MUTED_COLOR = (150, 158, 175)    # вторичный текст
GREEN = (0, 200, 120)            # прибыль
RED = (235, 68, 68)              # убыток
ACCENT = (88, 149, 255)          # акцент (имя/символ)

WIDTH = 640
_PAD = 28


def _font(size: int, bold: bool = False):
    path = r"C:/Windows/Fonts/arialbd.ttf" if bold else r"C:/Windows/Fonts/arial.ttf"
    try:
        return ImageFont.truetype(path, size)
    except Exception:
        return ImageFont.load_default()


def _draw_card(text: str):
    """Создаёт базовое изображение-карточку с заголовком и возвращает
    (img, draw, y) для последующей отрисовки строк."""
    img = Image.new("RGB", (WIDTH, 420), BG_COLOR)
    d = ImageDraw.Draw(img)
    d.rectangle([_PAD, _PAD - 6, WIDTH - _PAD, _PAD + 30], fill=ACCENT)
    # заголовок центрируем
    fh = _font(30, bold=True)
    bb = d.textbbox((0, 0), text, font=fh)
    tw = bb[2] - bb[0]
    d.text((_PAD + (WIDTH - 2 * _PAD - tw) / 2, _PAD), text, font=fh, fill=(255, 255, 255))
    # подложка под контент
    d.rectangle([_PAD, _PAD + 42, WIDTH - _PAD, 400], fill=PANEL_COLOR)
    return img, d, _PAD + 42 + 22


def _draw_row(d, y, label, value, label_color=MUTED_COLOR, value_color=TEXT_COLOR, value_font=None):
    d.text((_PAD + 18, y), label, font=_font(24), fill=label_color)
    vf = value_font or _font(26, bold=True)
    v = str(value)
    vb = d.textbbox((0, 0), v, font=vf)
    vw = vb[2] - vb[0]
    d.text((WIDTH - _PAD - 18 - vw, y + 2), v, font=vf, fill=value_color)
    return y + 44


def _build_base_card(title, rows):
    """rows: список (label, value) или (label, value, value_font, value_color)."""
    # сначала оценим высоту
    img = Image.new("RGB", (WIDTH, 420), BG_COLOR)
    d = ImageDraw.Draw(img)
    d.rectangle([_PAD, _PAD - 6, WIDTH - _PAD, _PAD + 30], fill=ACCENT)
    fh = _font(30, bold=True)
    bb = d.textbbox((0, 0), title, font=fh)
    tw = bb[2] - bb[0]
    d.text((_PAD + (WIDTH - 2 * _PAD - tw) / 2, _PAD), title, font=fh, fill=(255, 255, 255))
    d.rectangle([_PAD, _PAD + 42, WIDTH - _PAD, 400], fill=PANEL_COLOR)
    y = _PAD + 42 + 24
    for row in rows:
        label = row[0]
        value = row[1]
        vf = _font(26, bold=True)
        vc = TEXT_COLOR
        if len(row) >= 4 and row[3] is not None:
            vc = row[3]
        if len(row) >= 3 and row[2] is not None:
            vf = row[2]
        d.text((_PAD + 18, y), label, font=_font(24), fill=MUTED_COLOR)
        vtext = str(value)
        vb = d.textbbox((0, 0), vtext, font=vf)
        vw = vb[2] - vb[0]
        d.text((WIDTH - _PAD - 18 - vw, y + 2), vtext, font=vf, fill=vc)
        y += 44
    return img


class Notifier:
    """Отправка торговых сигналов и событий в Telegram в виде PNG-карточек.

    Использует python-telegram-bot (асинхронный telegram.Bot) и Pillow для
    рендера картинок. Если TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы —
    отправка игнорируется с логом WARNING (не влияет на торговлю).
    """

    def __init__(self, token: str = None, chat_id: str = None, connect_url: str = None):
        self.token = token if token else os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id if chat_id else os.getenv("TELEGRAM_CHAT_ID")
        self.connect_url = connect_url if connect_url else os.getenv("TELEGRAM_CONNECT_URL")
        self.bot = None
        if self.token and self.chat_id:
            self.bot = Bot(token=self.token)
            logger.info("Telegram Notifier initialized successfully.")
        else:
            logger.warning(
                "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. "
                "Telegram notifications will be ignored."
            )

    def _connect_keyboard(self):
        if not self.connect_url:
            return None
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("ПОДКЛЮЧИТЬ БОТ", url=self.connect_url)]
        ])

    async def _send_photo_with_retry(self, img: Image.Image, retries: int = 3, delay: int = 1):
        if not self.bot:
            return
        bio = io.BytesIO()
        img.save(bio, format="PNG")
        bio.seek(0)
        kb = self._connect_keyboard()
        for attempt in range(retries):
            try:
                await self.bot.send_photo(chat_id=self.chat_id, photo=bio, reply_markup=kb)
                logger.debug(f"Photo sent to Telegram (attempt {attempt + 1})")
                return
            except Exception as e:
                logger.error(f"Error sending photo to Telegram (attempt {attempt + 1}/{retries}): {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
        logger.error(f"Failed to send photo to Telegram after {retries} attempts")

    async def _send_text_with_retry(self, text: str, retries: int = 3, delay: int = 1):
        if not self.bot:
            return
        kb = self._connect_keyboard()
        for attempt in range(retries):
            try:
                await self.bot.send_message(chat_id=self.chat_id, text=text, reply_markup=kb)
                logger.debug(f"Message sent to Telegram (attempt {attempt + 1}): {text}")
                return
            except Exception as e:
                logger.error(f"Error sending message to Telegram (attempt {attempt + 1}/{retries}): {e}")
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
        logger.error(f"Failed to send message to Telegram after {retries} attempts: {text}")

    async def send_message(self, text: str):
        """Отправка произвольного текстового сообщения без блокировки цикла."""
        if not self.bot:
            return
        asyncio.create_task(self._send_text_with_retry(text))

    def _price_pct(self, entry, exit_, direction="LONG"):
        """Ценовой процент движения цены (exit-entry)/entry*100, для SHORT инвертированный."""
        try:
            e = float(entry)
            x = float(exit_)
        except (TypeError, ValueError):
            return None
        if e == 0:
            return None
        base = (x - e) / e * 100.0
        pct = base if direction.upper() == "LONG" else -base
        return pct

    def send_signal(self, signal_data: dict):
        """Форматирует сигнал в PNG-карточку и отправляет (fire-and-forget)."""
        if not self.bot:
            return

        direction = str(signal_data.get('direction', 'LONG')).upper()
        symbol = signal_data.get('symbol', 'N/A')
        entry_price = signal_data.get('entry_price', 'N/A')
        sl_price = signal_data.get('sl_price', 'N/A')
        tp1_price = signal_data.get('tp1_price', 'N/A')
        tp2_price = signal_data.get('tp2_price', 'N/A')
        leverage = signal_data.get('leverage', 'N/A')

        arrow = "▲" if direction == "LONG" else "▼"
        title = f"{arrow} {symbol} {direction}"
        img = _build_base_card(title, [
            ("Entry", entry_price),
            ("SL", sl_price),
            ("TP1", tp1_price),
            ("TP2", tp2_price),
            ("Leverage", f"{leverage}x"),
        ])
        asyncio.create_task(self._send_photo_with_retry(img))

    def send_event(self, event_type: str, details: dict):
        """Форматирует событие в PNG-карточку и отправляет (fire-and-forget)."""
        if not self.bot:
            return

        symbol = details.get('symbol', 'N/A')
        direction = str(details.get('direction', 'LONG')).upper()
        entry_price = details.get('entry_price', 'N/A')
        exit_price = details.get('exit_price', 'N/A')
        pnl = details.get('pnl', 'N/A')
        chainId = details.get('chainId', 'N/A')
        debt = details.get('debt', 'N/A')

        if event_type == "position_opened":
            arrow = "▲" if direction == "LONG" else "▼"
            img = _build_base_card(f"✅ OPEN {symbol}", [
                ("Direction", direction, None, ACCENT),
                ("Entry", entry_price),
            ])
        elif event_type in ("tp1_hit", "tp2_hit", "sl_hit"):
            label = event_type.replace("_hit", "").upper()
            emoji = "❌" if event_type == "sl_hit" else "🎯"
            pct = self._price_pct(entry_price, exit_price, direction)
            pnl_str = f"{pct:+.2f}%" if pct is not None else "N/A"
            # PnL: большой зелёный при прибыли, маленький красный при убытке
            is_profit = pct is not None and pct >= 0
            pnl_font = _font(52, bold=True) if is_profit else _font(34, bold=True)
            pnl_color = GREEN if is_profit else RED
            img = _build_base_card(f"{emoji} {label} {symbol}", [
                ("Direction", direction, None, ACCENT),
                ("Entry", entry_price),
                ("Exit", exit_price),
            ])
            # нарисуем крупный PnL ниже
            d = ImageDraw.Draw(img)
            ptext = f"PnL {pnl_str}"
            pb = d.textbbox((0, 0), ptext, font=pnl_font)
            pw = pb[2] - pb[0]
            d.text((WIDTH / 2 - pw / 2, 300), ptext, font=pnl_font, fill=pnl_color)
        elif event_type == "recovery":
            img = _build_base_card("🔄 RECOVERY", [
                ("Symbol", symbol, None, ACCENT),
                ("Chain", chainId),
                ("Debt", debt),
            ])
        elif event_type == "signal_rejected":
            reason = details.get('reason', 'risk_block')
            img = _build_base_card("⛔ REJECTED", [
                ("Symbol", symbol, None, ACCENT),
                ("Reason", reason),
            ])
        else:
            logger.warning(f"Unknown event type: {event_type}")
            return

        asyncio.create_task(self._send_photo_with_retry(img))
