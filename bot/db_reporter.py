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

    async def report_trade(self, trade: dict) -> Optional[int]:
        """Записывает новую сделку. Возвращает ID созданной записи."""
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
        except Exception as e:
            self.log.debug(f"[REPORTER] trade error: {e}")
        return None

    async def patch_trade(self, trade_id: int, data: dict) -> None:
        """Обновляет существующую сделку (закрытие)."""
        session = await self._get_session()
        if session is None:
            return
        try:
            async with session.patch(
                f"{API_URL}/trades/{trade_id}",
                json=data,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status >= 400:
                    self.log.debug(f"[REPORTER] trade PATCH failed: {resp.status}")
        except Exception as e:
            self.log.debug(f"[REPORTER] patch_trade error: {e}")

    async def _patch(self, data: dict) -> None:
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
        except Exception as e:
            self.log.debug(f"[REPORTER] error: {e}")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
