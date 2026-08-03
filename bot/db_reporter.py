"""
Отправляет состояние бота в API сервер по HTTP.
"""
import asyncio
import logging
import os
from typing import Optional

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

API_URL = os.getenv("DASHBOARD_API_URL", "http://localhost:5000/api")


class DbReporter:
    def __init__(self, symbol: str, logger: logging.Logger):
        self.symbol = symbol
        self.log = logger
        self._session: Optional["aiohttp.ClientSession"] = None

    async def _get_session(self):
        if not HAS_AIOHTTP:
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def report_heartbeat(self, price: float) -> None:
        await self._patch({"current_price": price, "is_running": True})

    async def report_position(self, position_dict: Optional[dict]) -> None:
        await self._patch({"position": position_dict})

    async def report_stopped(self) -> None:
        await self._patch({"is_running": False, "position": None})

    async def report_rejected(self, signal_data: dict, reason: str) -> None:
        """Записывает сигнал, отклонённый риск-контролем, как сделку со статусом 'rejected'
        (для статистики). pnl=0, is_open=0 — не влияет на торговую статистику."""
        import datetime as _dt
        payload = {
            "symbol": self.symbol,
            "direction": signal_data.get("direction", "LONG"),
            "entry_price": signal_data.get("entry_price", 0.0),
            "exit_price": None,
            "sl_price": signal_data.get("sl_price", 0.0),
            "tp1_price": signal_data.get("tp1_price", 0.0),
            "tp2_price": signal_data.get("tp2_price", 0.0),
            "qty": 0.0,
            "pnl": 0.0,
            "exit_reason": None,
            "entry_time": _dt.datetime.utcnow().isoformat(),
            "exit_time": None,
            "is_open": False,
            "mode": "live",
            "status": "rejected",
            "reject_reason": reason,
        }
        if signal_data.get("ema_fast") is not None:
            payload["ema_fast"] = signal_data.get("ema_fast")
            payload["ema_slow"] = signal_data.get("ema_slow")
            payload["volume"] = signal_data.get("volume")
            payload["volume_ma"] = signal_data.get("volume_ma")
        await self._post_trade(payload)

    async def _post_trade(self, trade: dict) -> None:
        for attempt in range(3):
            session = await self._get_session()
            if session is None:
                return
            try:
                async with session.post(
                    f"{API_URL}/trades",
                    json=trade,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status in (200, 201):
                        return
                    self.log.debug(f"[REPORTER] rejected trade POST failed: {resp.status}")
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.log.debug(f"[REPORTER] rejected trade attempt {attempt+1} error: {e}")
                await self._close_session()
                if attempt < 2:
                    await asyncio.sleep(0.5)

    async def report_trade(self, trade: dict) -> Optional[int]:
        """Записывает новую сделку. Возвращает ID созданной записи."""
        for attempt in range(3):
            session = await self._get_session()
            if session is None:
                return None
            try:
                async with session.post(
                    f"{API_URL}/trades",
                    json=trade,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        return data.get("id")
                    else:
                        self.log.debug(f"[REPORTER] trade POST failed: {resp.status}")
                        return None
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.log.debug(f"[REPORTER] trade attempt {attempt+1} error: {e}")
                await self._close_session()
                if attempt < 2:
                    await asyncio.sleep(0.5)
        return None

    async def patch_trade(self, trade_id: int, data: dict) -> bool:
        """Обновляет существующую сделку (закрытие). Возвращает True если успешно."""
        for attempt in range(3):
            session = await self._get_session()
            if session is None:
                return False
            try:
                async with session.patch(
                    f"{API_URL}/trades/{trade_id}",
                    json=data,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status >= 400:
                        self.log.debug(f"[REPORTER] trade PATCH failed: {resp.status}")
                        return False
                    return True
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.log.debug(f"[REPORTER] patch_trade attempt {attempt+1} error: {e}")
                await self._close_session()
                if attempt < 2:
                    await asyncio.sleep(0.5)
        return False

    async def _patch(self, data: dict) -> None:
        # Retry across API restarts: on a connection error, rebuild the
        # session (the old one holds a dead connection after the server
        # restarts) and try again a few times.
        for attempt in range(3):
            session = await self._get_session()
            if session is None:
                return
            try:
                async with session.patch(
                    f"{API_URL}/bots/{self.symbol}",
                    json=data,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status >= 400:
                        self.log.debug(f"[REPORTER] PATCH failed: {resp.status}")
                    return
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
                self.log.debug(f"[REPORTER] PATCH attempt {attempt+1} error: {e}")
                # Session likely stale after an API restart — drop and rebuild it.
                await self._close_session()
                if attempt < 2:
                    await asyncio.sleep(0.5)

    async def _close_session(self) -> None:
        if self._session and not self._session.closed:
            try:
                await self._session.close()
            except Exception:
                pass
        self._session = None

    async def close(self) -> None:
        await self._close_session()
