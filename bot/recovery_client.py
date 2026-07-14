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

# Server-to-server токен для RBAC на API сервере.
INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")


def _auth_headers() -> dict:
    return {"x-internal-token": INTERNAL_API_TOKEN} if INTERNAL_API_TOKEN else {}

# ── Config path ───────────────────────────────────────────────────────────
_CONFIG_PATH = os.getenv("RECOVERY_CONFIG_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "recovery_config.yaml"))


def readRecoveryConfig() -> dict:
    """Читает recovery_config.yaml при каждом вызове (без кэша)."""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            import yaml as _yaml
            raw = _yaml.safe_load(f) or {}
        return {
            "recovery_enabled": bool(raw.get("recovery_enabled", False)),
            "recovery_bonus_pct": float(raw.get("recovery_bonus_pct", 0)),
            "recovery_max_pct": float(raw.get("recovery_max_pct", 50.0)),
        }
    except Exception:
        return {"recovery_enabled": False, "recovery_bonus_pct": 0, "recovery_max_pct": 50.0}


class RecoveryClient:
    def __init__(self, symbol: str, logger: logging.Logger):
        self.symbol = symbol
        self.log = logger
        self._session: Optional["aiohttp.ClientSession"] = None

    async def _get_session(self):
        if not HAS_AIOHTTP:
            self.log.warning("[RECOVERY] aiohttp not available, recovery disabled")
            return None
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def claim(self) -> dict:
        """
        Пытается захватить свободный долг перед открытием новой позиции.
        Возвращает: {"chainId": int|None, "debtAmount": float, "bonusPct": float, "enabled": bool}
        """
        default = {"chainId": None, "debtAmount": 0.0, "bonusPct": 0.0, "enabled": False}
        session = await self._get_session()
        if session is None:
            self.log.debug("[RECOVERY] claim: no session (aiohttp unavailable)")
            return default
        try:
            self.log.info(f"[RECOVERY] claim: POST {API_URL}/recovery/claim symbol={self.symbol}")
            async with session.post(
                f"{API_URL}/recovery/claim",
                json={"symbol": self.symbol},
                headers=_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                data = await resp.json()
                self.log.info(f"[RECOVERY] claim: response status={resp.status} data={data}")
                if resp.status == 200:
                    return data
                self.log.warning(f"[RECOVERY] claim failed: status={resp.status}")
        except Exception as e:
            self.log.error(f"[RECOVERY] claim error: {e}")
        return default

    async def report(self, pnl: float, chain_id: Optional[int] = None) -> None:
        """Сообщает результат закрытой сделки."""
        session = await self._get_session()
        if session is None:
            return
        try:
            payload = {"symbol": self.symbol, "pnl": pnl}
            if chain_id is not None:
                payload["chainId"] = chain_id
            self.log.info(f"[RECOVERY] report: POST {API_URL}/recovery/report payload={payload}")
            async with session.post(
                f"{API_URL}/recovery/report",
                json=payload,
                headers=_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self.log.info(f"[RECOVERY] report: success data={data}")
                else:
                    self.log.warning(f"[RECOVERY] report failed: status={resp.status}")
        except Exception as e:
            self.log.error(f"[RECOVERY] report error: {e}")

    async def release(self, chain_id: int) -> None:
        """Освобождает захваченную цепочку (переводит обратно в free)."""
        session = await self._get_session()
        if session is None:
            return
        try:
            async with session.post(
                f"{API_URL}/recovery/release",
                json={"symbol": self.symbol, "chainId": chain_id},
                headers=_auth_headers(),
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    self.log.info(f"[RECOVERY] Released chain #{chain_id}")
                else:
                    self.log.warning(f"[RECOVERY] release failed: {resp.status}")
        except Exception as e:
            self.log.error(f"[RECOVERY] release error: {e}")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
