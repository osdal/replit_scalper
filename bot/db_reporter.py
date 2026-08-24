"""
Отправляет состояние бота в API сервер по HTTP.
"""
import asyncio
import logging
import math
import os
import socket
from typing import Optional

try:
    import aiohttp
    HAS_AIOHTTP = True
except ImportError:
    HAS_AIOHTTP = False

API_URL = os.getenv("DASHBOARD_API_URL", "http://localhost:5000/api")


def _clean_nums(obj):
    """Рекурсивно заменяет float NaN/Infinity на None.

    aiohttp сериализует NaN/Inf как 'NaN'/'Infinity' (расширение Python json),
    а Express strict-JSON парсер отвергает такой JSON -> 400 HTML. Индикаторы
    сигнала (rsi/atr/macd/...) могут быть NaN на коротких данных, поэтому
    очищаем перед отправкой."""
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _clean_nums(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_nums(v) for v in obj]
    return obj



class DbReporter:
    def __init__(self, symbol: str, logger: logging.Logger):
        self.symbol = symbol
        self.log = logger
        self._session: Optional["aiohttp.ClientSession"] = None

    async def _get_session(self):
        if not HAS_AIOHTTP:
            return None
        if self._session is None or self._session.closed:
            # trust_env=False + принудительный IPv4: бот ходит на локальный API,
            # прокси и резолв localhost->IPv6 здесь не нужны (источник лишних сбоев).
            connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=False)
            self._session = aiohttp.ClientSession(connector=connector, trust_env=False)
        return self._session

    async def report_heartbeat(self, price: float) -> None:
        await self._patch({"current_price": price, "is_running": True})

    async def report_position(self, position_dict: Optional[dict]) -> None:
        await self._patch({"position": position_dict})

    async def report_stopped(self) -> None:
        await self._patch({"is_running": False, "position": None})

    async def report_rejected(self, signal_data: dict, reason: str, qty: float = 0.0, mode: str = "paper") -> Optional[int]:
        """Записывает сигнал, отклонённый риск-контролем, как сделку со статусом 'rejected'
        (для статистики). pnl=0, is_open=0 — не влияет на торговую статистику.
        Возвращает id созданной записи (для последующей симуляции TP/SL)."""
        import datetime as _dt
        payload = {
            "symbol": self.symbol,
            "direction": signal_data.get("direction", "LONG"),
            "entry_price": signal_data.get("entry_price", 0.0),
            "exit_price": None,
            "sl_price": signal_data.get("sl_price", 0.0),
            "tp1_price": signal_data.get("tp1_price", 0.0),
            "tp2_price": signal_data.get("tp2_price", 0.0),
            "qty": qty,
            "pnl": 0.0,
            "exit_reason": None,
            "entry_time": _dt.datetime.utcnow().isoformat(),
            "exit_time": None,
            "is_open": False,
            "mode": mode,
            "status": "rejected",
            "reject_reason": reason,
        }
        if signal_data.get("ema_fast") is not None:
            payload["ema_fast"] = signal_data.get("ema_fast")
            payload["ema_slow"] = signal_data.get("ema_slow")
            payload["volume"] = signal_data.get("volume")
            payload["volume_ma"] = signal_data.get("volume_ma")
        payload["preset"] = signal_data.get("preset")
        payload["rsi"] = signal_data.get("rsi")
        payload["macd"] = signal_data.get("macd")
        payload["macd_signal"] = signal_data.get("macd_signal")
        payload["macd_hist"] = signal_data.get("macd_hist")
        payload["bb_upper"] = signal_data.get("bb_upper")
        payload["bb_middle"] = signal_data.get("bb_middle")
        payload["bb_lower"] = signal_data.get("bb_lower")
        payload["atr"] = signal_data.get("atr")
        payload["quote_volume"] = signal_data.get("quote_volume")
        return await self._post_trade(payload)

    async def _post_trade(self, trade: dict) -> Optional[int]:
        for attempt in range(3):
            session = await self._get_session()
            if session is None:
                return None
            try:
                payload = _clean_nums(dict(trade))
                payload.setdefault("symbol", self.symbol)
                payload.setdefault("rsi", trade.get("rsi"))
                payload.setdefault("macd", trade.get("macd"))
                payload.setdefault("macd_signal", trade.get("macd_signal"))
                payload.setdefault("macd_hist", trade.get("macd_hist"))
                payload.setdefault("bb_upper", trade.get("bb_upper"))
                payload.setdefault("bb_middle", trade.get("bb_middle"))
                payload.setdefault("bb_lower", trade.get("bb_lower"))
                payload.setdefault("atr", trade.get("atr"))
                payload.setdefault("preset", trade.get("preset"))
                async with session.post(
                    f"{API_URL}/trades",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        return data.get("id")
                    body = ""
                    try:
                        body = await resp.text()
                    except Exception:
                        pass
                    self.log.warning(f"[REPORTER] rejected trade POST failed: {resp.status} body={body[:300]} url={API_URL}/trades")
                    # Не сдаёмся сразу на не-2xx — сервер под нагрузкой может
                    # временно отвечать ошибкой. Повторяем в рамках цикла попыток.
                    if attempt < 2:
                        await asyncio.sleep(0.5)
                        continue
                    return None
            except Exception as e:
                self.log.warning(f"[REPORTER] rejected trade attempt {attempt+1} error: {type(e).__name__}: {e} url={API_URL}/trades")
                await self._close_session()
                if attempt < 2:
                    await asyncio.sleep(0.5)
        return None

    async def report_trade(self, trade: dict) -> Optional[int]:
        """Записывает новую сделку. Возвращает ID созданной записи."""
        for attempt in range(3):
            session = await self._get_session()
            if session is None:
                return None
            try:
                payload = _clean_nums(dict(trade))
                payload.setdefault("rsi", trade.get("rsi"))
                payload.setdefault("macd", trade.get("macd"))
                payload.setdefault("macd_signal", trade.get("macd_signal"))
                payload.setdefault("macd_hist", trade.get("macd_hist"))
                payload.setdefault("bb_upper", trade.get("bb_upper"))
                payload.setdefault("bb_middle", trade.get("bb_middle"))
                payload.setdefault("bb_lower", trade.get("bb_lower"))
                payload.setdefault("atr", trade.get("atr"))
                payload.setdefault("preset", trade.get("preset"))
                async with session.post(
                    f"{API_URL}/trades",
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status in (200, 201):
                        data = await resp.json()
                        return data.get("id")
                    else:
                        self.log.debug(f"[REPORTER] trade POST failed: {resp.status}")
                        return None
            except Exception as e:
                self.log.debug(f"[REPORTER] trade attempt {attempt+1} error: {e}")
                await self._close_session()
                if attempt < 2:
                    await asyncio.sleep(0.5)
        return None

    async def patch_trade(self, trade_id: int, data: dict) -> bool:
        """Обновляет существующую сделку (закрытие). Возвращает True если успешно."""
        data = _clean_nums(dict(data))
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
                        body = ""
                        try:
                            body = await resp.text()
                        except Exception:
                            pass
                        self.log.warning(f"[REPORTER] trade PATCH failed: {resp.status} body={body[:200]} id={trade_id} url={API_URL}/trades/{trade_id}")
                        return False
                    return True
            except Exception as e:
                self.log.warning(f"[REPORTER] patch_trade attempt {attempt+1} error: {type(e).__name__}: {e} id={trade_id}")
                await self._close_session()
                if attempt < 2:
                    await asyncio.sleep(0.5)
        return False

    async def get_trade(self, trade_id: int) -> Optional[dict]:
        """Возвращает сделку по id из БД (для получения entry_time восстановленных позиций)."""
        for attempt in range(3):
            session = await self._get_session()
            if session is None:
                return None
            try:
                async with session.get(
                    f"{API_URL}/trades/{trade_id}",
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status == 200:
                        return await resp.json()
                    return None
            except Exception as e:
                self.log.debug(f"[REPORTER] get_trade attempt {attempt+1} error: {e}")
                await self._close_session()
                if attempt < 2:
                    await asyncio.sleep(0.5)
        return None

    async def get_pending_rejected_trades(self) -> list[dict]:
        """Возвращает все отклонённые сделки без exit_reason (ожидают симуляции)."""
        session = await self._get_session()
        if not session:
            return []
        try:
            async with session.get(
                f"{API_URL}/trades",
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return [
                        t for t in data.get("trades", [])
                        if t.get("status") == "rejected" and not t.get("exit_reason")
                        and not str(t.get("reject_reason") or "").startswith("skip:")
                    ]
        except Exception as e:
            self.log.debug(f"[REPORTER] get_pending_rejected_trades error: {e}")
        return []

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
                    json=_clean_nums(dict(data)),
                    timeout=aiohttp.ClientTimeout(total=5),
                ) as resp:
                    if resp.status >= 400:
                        self.log.warning(f"[REPORTER] bot PATCH failed: {resp.status} url={API_URL}/bots/{self.symbol}")
                    return
            except Exception as e:
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
