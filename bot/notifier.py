
import os
import asyncio
import logging

from telegram import Bot

logger = logging.getLogger(__name__)


class Notifier:
    """Отправка торговых сигналов и событий в Telegram.

    Использует python-telegram-bot (асинхронный telegram.Bot).
    Если TELEGRAM_BOT_TOKEN или TELEGRAM_CHAT_ID не заданы — отправка
    игнорируется с логом WARNING (не влияет на торговлю).
    """

    def __init__(self, token: str = None, chat_id: str = None):
        self.token = token if token else os.getenv("TELEGRAM_BOT_TOKEN")
        self.chat_id = chat_id if chat_id else os.getenv("TELEGRAM_CHAT_ID")
        self.bot = None
        if self.token and self.chat_id:
            self.bot = Bot(token=self.token)
            logger.info("Telegram Notifier initialized successfully.")
        else:
            logger.warning(
                "TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID not set. "
                "Telegram notifications will be ignored."
            )

    async def _send_with_retry(self, text: str, retries: int = 3, delay: int = 1):
        """Отправка с автоматическими повторами при ошибках."""
        if not self.bot:
            return

        for attempt in range(retries):
            try:
                await self.bot.send_message(chat_id=self.chat_id, text=text)
                logger.debug(f"Message sent to Telegram (attempt {attempt + 1}): {text}")
                return
            except Exception as e:
                logger.error(
                    f"Error sending message to Telegram (attempt {attempt + 1}/{retries}): {e}"
                )
                if attempt < retries - 1:
                    await asyncio.sleep(delay)
        logger.error(f"Failed to send message to Telegram after {retries} attempts: {text}")

    async def send_message(self, text: str):
        """Отправка текстового сообщения без блокировки основного цикла."""
        if not self.bot:
            return
        asyncio.create_task(self._send_with_retry(text))

    def send_signal(self, signal_data: dict):
        """Форматирует и отправляет сигнал (fire-and-forget)."""
        if not self.bot:
            return

        direction = signal_data.get('direction', 'N/A').upper()
        symbol = signal_data.get('symbol', 'N/A')
        entry_price = signal_data.get('entry_price', 'N/A')
        sl_price = signal_data.get('sl_price', 'N/A')
        tp1_price = signal_data.get('tp1_price', 'N/A')
        tp2_price = signal_data.get('tp2_price', 'N/A')

        ema_fast = signal_data.get('ema_fast')
        ema_slow = signal_data.get('ema_slow')
        volume = signal_data.get('volume')
        volume_ma = signal_data.get('volume_ma')
        leverage = signal_data.get('leverage', 'N/A')

        ema_fast_str = f"{ema_fast:.2f}" if ema_fast is not None else "N/A"
        ema_slow_str = f"{ema_slow:.2f}" if ema_slow is not None else "N/A"

        message = (
            f"🔔 {direction} {symbol}\n"
            f"Entry: {entry_price}\n"
            f"SL: {sl_price} | TP1: {tp1_price} | TP2: {tp2_price}\n"
            f"EMA fast: {ema_fast_str} | slow: {ema_slow_str}\n"
            f"Volume: {volume} (MA: {volume_ma})\n"
            f"Leverage: {leverage}x"
        )
        asyncio.create_task(self._send_with_retry(message))

    def send_event(self, event_type: str, details: dict):
        """Форматирует и отправляет событие (fire-and-forget)."""
        if not self.bot:
            return

        symbol = details.get('symbol', 'N/A')
        qty = details.get('qty', 'N/A')
        pnl = details.get('pnl', 'N/A')
        price = details.get('price', 'N/A')
        direction = details.get('direction', 'N/A').upper()
        chainId = details.get('chainId', 'N/A')
        debt = details.get('debt', 'N/A')

        message = ""
        if event_type == "position_opened":
            message = f"✅ Position opened | {symbol} {direction} qty={qty} entry={price}"
        elif event_type == "tp1_hit":
            message = f"🎯 TP1 hit | {symbol} qty={qty} pnl={pnl}"
        elif event_type == "tp2_hit":
            message = f"🎯 TP2 hit | {symbol} qty={qty} pnl={pnl}"
        elif event_type == "sl_hit":
            message = f"❌ SL hit | {symbol} qty={qty} pnl={pnl}"
        elif event_type == "recovery":
            message = f"🔄 Recovery | {symbol} chainId={chainId} debt={debt}"
        elif event_type == "signal_rejected":
            reason = details.get('reason', 'risk_block')
            message = f"⛔ Signal rejected | {symbol} reason={reason}"
        else:
            logger.warning(f"Unknown event type: {event_type}")
            return

        asyncio.create_task(self._send_with_retry(message))
