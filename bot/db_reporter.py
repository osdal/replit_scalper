"""
Отправляет состояние бота в API сервер по HTTP.
Не требует прямого подключения к БД — работает через REST API.
"""
import asyncio
import json
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
    """Асинхронно репортит состояние бота в дашборд."""

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

    async def report_trade(self, trade: dict) -> None:
        """Записывает сделку в историю."""
        session = await self._get_session()
        if session is None:
            return
        try:
            async with session.post(
                f"{API_URL}/trades",
                json=trade,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status >= 400:
                    self.log.warning(f"[REPORTER] trade POST failed: {resp.status}")
        except Exception as e:
            self.log.debug(f"[REPORTER] trade error: {e}")

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
