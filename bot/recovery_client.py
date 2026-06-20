"""
Клиент для координации режима компенсации убытков (recovery mode)
через центральный API сервер. Несколько ботов работают как отдельные
процессы — захват "свободного долга" происходит атомарно на сервере.
"""
import logging
import os
from typing import Optional

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

API_URL = os.getenv("DASHBOARD_API_URL", "http://localhost:5000/api")


class RecoveryClient:
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

    async def claim(self) -> dict:
        """
        Пытается захватить свободный долг перед открытием новой позиции.
        Возвращает: {"chainId": int|None, "debtAmount": float, "bonusPct": float, "enabled": bool}
        Если API недоступен или recovery выключен — chainId=None, enabled=False.
        """
        default = {"chainId": None, "debtAmount": 0.0, "bonusPct": 0.0, "enabled": False}
        session = await self._get_session()
        if session is None:
            return default
        try:
            async with session.post(
                f"{API_URL}/recovery/claim",
                json={"symbol": self.symbol},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                self.log.debug(f"[RECOVERY] claim failed: {resp.status}")
        except Exception as e:
            self.log.debug(f"[RECOVERY] claim error: {e}")
        return default

    async def report(self, pnl: float, chain_id: Optional[int] = None) -> None:
        """
        Сообщает результат закрытой сделки.
        chain_id передаётся если эта сделка была компенсатором.
        """
        session = await self._get_session()
        if session is None:
            return
        try:
            payload = {"symbol": self.symbol, "pnl": pnl}
            if chain_id is not None:
                payload["chainId"] = chain_id
            async with session.post(
                f"{API_URL}/recovery/report",
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status != 200:
                    self.log.debug(f"[RECOVERY] report failed: {resp.status}")
        except Exception as e:
            self.log.debug(f"[RECOVERY] report error: {e}")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
