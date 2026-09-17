import asyncio
import json
import logging
import random
import time
from typing import Callable, Dict, Optional

import pandas as pd
from binance import AsyncClient, BinanceSocketManager

from rate_limit import with_retry

logger = logging.getLogger("market_data")

# WS-цена считается свежей не дольше этого порога; после — падаем на REST ticker.
PRICE_WS_MAX_AGE_SEC = 5.0
# Окно дедупликации klines: одинаковый запрос в пределах окна берётся из кэша,
# чтобы не делать одну и ту же работу дважды в одном цикле свечи.
KLINES_DEDUPE_TTL_SEC = 3.0
_KLINES_CACHE_MAX_KEYS = 64

_klines_cache: Dict[tuple, tuple] = {}
_klines_locks: Dict[tuple, asyncio.Lock] = {}


def _prune_klines_cache() -> None:
    if len(_klines_cache) <= _KLINES_CACHE_MAX_KEYS:
        return
    oldest = sorted(_klines_cache.items(), key=lambda kv: kv[1][0])
    for key, _ in oldest[: _KLINES_CACHE_MAX_KEYS // 4]:
        _klines_cache.pop(key, None)


async def get_historical_klines(
    client: AsyncClient,
    symbol: str,
    interval: str,
    start: str,
    end: Optional[str] = None,
) -> pd.DataFrame:
    klines = await client.futures_historical_klines(
        symbol=symbol,
        interval=interval,
        start_str=start,
        end_str=end,
    )
    return _klines_to_df(klines)


async def get_recent_klines(
    client: AsyncClient,
    symbol: str,
    interval: str,
    limit: int = 200,
    start_ms: Optional[int] = None,
) -> pd.DataFrame:
    key = (symbol, interval, limit, start_ms)
    cached = _klines_cache.get(key)
    if cached is not None and (time.monotonic() - cached[0]) < KLINES_DEDUPE_TTL_SEC:
        logger.debug(f"[CACHE] klines hit | {symbol} {interval} limit={limit}")
        return cached[1].copy()

    lock = _klines_locks.setdefault(key, asyncio.Lock())
    async with lock:
        # Повторная проверка под локом — другой корутин мог уже сходить за свечами.
        cached = _klines_cache.get(key)
        if cached is not None and (time.monotonic() - cached[0]) < KLINES_DEDUPE_TTL_SEC:
            logger.debug(f"[CACHE] klines hit | {symbol} {interval} limit={limit}")
            return cached[1].copy()

        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": limit,
        }
        if start_ms is not None:
            params["startTime"] = start_ms

        async def _fetch():
            return await client.futures_klines(**params)

        try:
            klines = await asyncio.wait_for(with_retry(_fetch, log=logger), timeout=120)
        except asyncio.TimeoutError:
            if logger:
                logger.error(f"[POLL] get_recent_klines timeout for {symbol} {interval}")
            raise
        df = _klines_to_df(klines)
        _klines_cache[key] = (time.monotonic(), df)
        _prune_klines_cache()
        # Копия — чтобы caller/индикаторы не мутировали закэшированный DataFrame.
        return df.copy()


async def get_current_price(
    client: AsyncClient,
    symbol: str,
    ws_price: float = 0.0,
    ws_ts: float = 0.0,
    logger: Optional[logging.Logger] = None,
    max_age: float = PRICE_WS_MAX_AGE_SEC,
) -> float:
    """
    Цена для SL/TP-тика и heartbeat.

    Предпочитает WS-цену (markPrice), пока она присутствует и не старше max_age;
    REST ticker дёргается ТОЛЬКО если WS-значение отсутствует или устарело.
    Никогда не бросает: при ошибке REST возвращает последнюю известную WS-цену
    (или 0.0), чтобы не ронять SL/TP-тик.
    """
    price = float(ws_price or 0.0)
    ws_ts = float(ws_ts or 0.0)
    age = time.time() - ws_ts
    has_ws = price > 0
    if has_ws and ws_ts > 0 and age <= max_age:
        return price

    if logger:
        if not has_ws:
            logger.debug(f"[PRICE] WS price missing for {symbol}, falling back to REST ticker")
        else:
            logger.debug(
                f"[PRICE] WS price stale ({age:.1f}s > {max_age:.0f}s) for {symbol}, "
                f"falling back to REST ticker"
            )
    try:
        ticker = await client.futures_symbol_ticker(symbol=symbol)
        rest_price = float(ticker.get("price", 0) or 0)
        if rest_price > 0:
            return rest_price
    except Exception as e:
        if logger:
            logger.warning(f"[PRICE] REST ticker failed for {symbol}: {e}")
    # Фолбэк — последняя известная цена (никогда не роняем тик).
    return price if price > 0 else 0.0


def _klines_to_df(klines: list) -> pd.DataFrame:
    columns = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(klines, columns=columns)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    df["close_time"] = pd.to_datetime(df["close_time"], unit="ms")
    for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[col] = df[col].astype(float)
    df.set_index("open_time", inplace=True)
    return df


def _ws_kline_to_series(k: dict) -> pd.Series:
    """Свеча из WS-сообщения (@kline). k — вложенный объект 'k' из события."""
    t = pd.to_datetime(k["t"], unit="ms")
    candle = pd.Series({
        "open_time": t,
        "open":  float(k["o"]),
        "high":  float(k["h"]),
        "low":   float(k["l"]),
        "close": float(k["c"]),
        "volume": float(k["v"]),
    })
    candle.name = t
    return candle


async def start_kline_websocket(
    client: AsyncClient,
    symbol: str,
    handlers: Dict[str, Callable],
    logger: Optional[logging.Logger] = None,
    shutdown_event: Optional[asyncio.Event] = None,
    on_price: Optional[Callable[[float], None]] = None,
    ping_timeout: int = 90,
) -> None:
    """
    WebSocket market data вместо REST-поллинга.

    Подписывается на kline-стримы по всем интервалам из `handlers` и на
    markPrice. Обработчик вызывается только на ЗАКРЫТОЙ свече (`k.x == True`) —
    контракт тот же, что у start_kline_polling: callback(pd.Series) с полями
    open/high/low/close/volume и .name = open_time (pd.Timestamp).

    on_price вызывается на каждом markPrice-апдейте — используется ботом для
    SL/TP-тика и heartbeat, чтобы не дёргать REST ticker.

    При обрыве — реконнект с экспоненциальным backoff.
    """
    bm = BinanceSocketManager(client)
    sym = symbol.lower()
    stream_to_interval: Dict[str, str] = {
        f"{sym}@kline_{iv}": iv for iv in handlers
    }
    streams = list(stream_to_interval.keys()) + [f"{sym}@markPrice@1s"]

    if logger:
        logger.info(
            f"[WS] Starting market data socket | {symbol} streams={streams}"
        )

    backoff = 1.0
    while True:
        if shutdown_event and shutdown_event.is_set():
            break
        try:
            # category=None — используем базовый URL (совместимо с testnet).
            async with bm.futures_multiplex_socket(streams, category=None) as stream:
                if logger:
                    logger.info(
                        f"[WS] Connected | {symbol} intervals={list(handlers.keys())}"
                    )
                while True:
                    if shutdown_event and shutdown_event.is_set():
                        break
                    try:
                        msg = await asyncio.wait_for(stream.recv(), timeout=ping_timeout)
                        # Соединение реально работает — только теперь сбрасываем backoff,
                        # чтобы не крутить 1-секундный реконнект при «открылся и упал».
                        backoff = 1.0
                    except asyncio.TimeoutError:
                        if logger:
                            logger.warning(
                                f"[WS] No message for {ping_timeout}s, reconnecting"
                            )
                        break
                    if not isinstance(msg, dict):
                        continue
                    data = msg.get("data", msg)
                    if not isinstance(data, dict):
                        continue
                    event = data.get("e")
                    if event == "kline":
                        k = data.get("k") or {}
                        if not k.get("x"):
                            continue
                        iv = stream_to_interval.get(msg.get("stream", ""))
                        cb = handlers.get(iv) if iv else None
                        if cb is None:
                            continue
                        candle = _ws_kline_to_series(k)
                        if logger:
                            logger.info(
                                f"Candle closed | {iv} "
                                f"time={candle.name} close={candle['close']:.2f}"
                            )
                        try:
                            result = cb(candle)
                            if asyncio.iscoroutine(result):
                                await result
                        except Exception as e:
                            if logger:
                                logger.error(
                                    f"Candle handler error ({iv}): {e}",
                                    exc_info=True,
                                )
                    elif event == "markPriceUpdate" and on_price is not None:
                        try:
                            price = float(data.get("p", 0) or 0)
                            if price > 0:
                                on_price(price)
                        except Exception:
                            pass
        except asyncio.CancelledError:
            raise
        except Exception as e:
            if logger:
                logger.warning(
                    f"[WS] socket error: {e}; reconnecting in {backoff:.0f}s"
                )
        if shutdown_event and shutdown_event.is_set():
            break
        await asyncio.sleep(backoff + random.random())
        backoff = min(backoff * 2, 60.0)


async def start_kline_polling(
    client: AsyncClient,
    symbol: str,
    handlers: Dict[str, Callable],
    logger: Optional[logging.Logger] = None,
    poll_seconds: int = 10,
    shutdown_event: Optional[asyncio.Event] = None,
    skip_catchup: bool = True,
) -> None:
    """
    REST polling — checks for new closed candles every poll_seconds.
    При старте инициализирует last_seen последней закрытой свечой,
    чтобы не прокручивать старые свечи после перезапуска.

    shutdown_event: если передан, polling выходит когда событие установлено.
    skip_catchup: если True (по умолчанию), пропускает все закрытые свечи
                  между last_seen и текущим моментом — бот стартует с
                  первой следующей свечи. False — обрабатывает все
                  пропущенные свечи по одной (полезно для backtest).
    """
    last_seen: Dict[str, pd.Timestamp] = {}

    if logger:
        logger.info(f"[POLL] Starting init for {symbol} intervals={list(handlers.keys())}")

    for interval in handlers:
        try:
            if logger:
                logger.info(f"[POLL] Fetching init klines for {symbol} {interval}")

            async def _init_fetch(interval=interval):
                return await client.futures_klines(symbol=symbol, interval=interval, limit=2)

            klines = await asyncio.wait_for(with_retry(_init_fetch, log=logger), timeout=120)
            if logger:
                logger.info(f"[POLL] Got {len(klines)} klines for {symbol} {interval}")
            df = _klines_to_df(klines)
            if skip_catchup:
                last_seen[interval] = df.iloc[-1].name
                if logger:
                    logger.info(
                        f"Polling init | {interval} last_seen={last_seen[interval]} "
                        f"(catch-up skipped, waiting for next candle)"
                    )
            else:
                last_seen[interval] = df.iloc[-2].name
                if logger:
                    logger.info(
                        f"Polling init | {interval} last_seen={last_seen[interval]}"
                    )
        except asyncio.TimeoutError:
            if logger:
                logger.error(f"[POLL] init timeout for {symbol} {interval}")
            raise
        except Exception as e:
            if logger:
                logger.error(f"Polling init error ({interval}): {e}")

    if logger:
        logger.info(
            f"[POLL] Polling started | intervals={list(handlers.keys())} "
            f"every {poll_seconds}s"
        )

    while True:
        # Проверяем shutdown_event перед каждой итерацией
        if shutdown_event and shutdown_event.is_set():
            if logger:
                logger.info("Shutdown event received, stopping polling")
            break
        
        for interval, callback in handlers.items():
            try:
                if logger:
                    logger.debug(f"[POLL] Fetching klines for {symbol} {interval}")

                async def _poll_fetch(interval=interval):
                    return await client.futures_klines(symbol=symbol, interval=interval, limit=2)

                klines = await asyncio.wait_for(with_retry(_poll_fetch, log=logger), timeout=120)
                if logger:
                    logger.debug(f"[POLL] Got {len(klines)} klines for {symbol} {interval}")
                df = _klines_to_df(klines)
                closed = df.iloc[-1]
                candle_time = closed.name

                if last_seen.get(interval) is None or candle_time > last_seen[interval]:
                    last_seen[interval] = candle_time
                    if logger:
                        logger.info(
                            f"Candle closed | {interval} "
                            f"time={candle_time} close={closed['close']:.2f}"
                        )
                    candle = pd.Series({
                        "open_time": candle_time,
                        "open":   closed["open"],
                        "high":   closed["high"],
                        "low":    closed["low"],
                        "close":  closed["close"],
                        "volume": closed["volume"],
                    })
                    candle.name = candle_time
                    try:
                        result = callback(candle)
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as e:
                        if logger:
                            logger.error(
                                f"Candle handler error ({interval}): {e}",
                                exc_info=True,
                            )

            except Exception as e:
                if logger:
                    logger.error(f"Polling error ({interval}): {e}")

        try:
            # Используем wait_for с timeout вместо sleep, чтобы проверять shutdown_event чаще
            await asyncio.wait_for(
                shutdown_event.wait() if shutdown_event else asyncio.sleep(poll_seconds),
                timeout=poll_seconds
            )
            if shutdown_event and shutdown_event.is_set():
                if logger:
                    logger.info("Shutdown event received during sleep, stopping polling")
                break
        except asyncio.TimeoutError:
            # Timeout ожидается, просто продолжаем цикл
            pass