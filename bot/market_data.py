import asyncio
import json
import logging
from typing import Callable, Dict, Optional

import pandas as pd
import websockets
from binance import AsyncClient

FUTURES_WS_URL = "wss://fstream.binance.com/ws"
FUTURES_WS_MULTI_URL = "wss://fstream.binance.com/stream"


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
) -> pd.DataFrame:
    klines = await client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    return _klines_to_df(klines)


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


def _kline_to_series(kline: dict) -> pd.Series:
    return pd.Series({
        "open_time": pd.to_datetime(kline["t"], unit="ms"),
        "open":      float(kline["o"]),
        "high":      float(kline["h"]),
        "low":       float(kline["l"]),
        "close":     float(kline["c"]),
        "volume":    float(kline["v"]),
    })


async def start_kline_socket(
    client: AsyncClient,
    symbol: str,
    interval: str,
    on_candle_close: Callable,
    logger: Optional[logging.Logger] = None,
    reconnect_delay: int = 5,
) -> None:
    """Single-stream WebSocket for one symbol/interval."""
    stream = f"{symbol.lower()}@kline_{interval}"
    url = f"{FUTURES_WS_URL}/{stream}"

    while True:
        try:
            if logger:
                logger.info(f"WebSocket connecting | {url}")
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                if logger:
                    logger.info(f"WebSocket connected | {interval}")
                async for raw in ws:
                    msg = json.loads(raw)
                    kline = msg.get("k", {})
                    if not kline.get("x"):
                        continue
                    if logger:
                        logger.info(f"WS candle closed | {interval}")
                    candle = _kline_to_series(kline)
                    result = on_candle_close(candle)
                    if asyncio.iscoroutine(result):
                        await result

        except (websockets.ConnectionClosed, websockets.WebSocketException) as e:
            if logger:
                logger.warning(f"WebSocket disconnected ({interval}): {e} — reconnecting in {reconnect_delay}s")
        except Exception as e:
            if logger:
                logger.error(f"WebSocket error ({interval}): {e} — reconnecting in {reconnect_delay}s")

        await asyncio.sleep(reconnect_delay)


async def start_multi_kline_socket(
    client: AsyncClient,
    symbol: str,
    handlers: Dict[str, Callable],
    logger: Optional[logging.Logger] = None,
    reconnect_delay: int = 5,
) -> None:
    """
    Multiplexed WebSocket — one connection for multiple intervals.
    handlers: { "5m": on_5m_candle_close, "1h": on_1h_candle_close }
    """
    streams = "/".join(f"{symbol.lower()}@kline_{iv}" for iv in handlers)
    url = f"{FUTURES_WS_MULTI_URL}?streams={streams}"

    while True:
        try:
            if logger:
                logger.info(f"WebSocket connecting (multi) | {url}")
            async with websockets.connect(url, ping_interval=20, ping_timeout=10) as ws:
                if logger:
                    logger.info(f"WebSocket connected | streams: {list(handlers.keys())}")
                async for raw in ws:
                    msg = json.loads(raw)
                    data = msg.get("data", {})
                    kline = data.get("k", {})
                    if not kline.get("x"):
                        continue
                    interval = kline.get("i")
                    callback = handlers.get(interval)
                    if callback is None:
                        continue
                    if logger:
                        logger.info(f"WS candle closed | {interval}")
                    candle = _kline_to_series(kline)
                    result = callback(candle)
                    if asyncio.iscoroutine(result):
                        await result

        except (websockets.ConnectionClosed, websockets.WebSocketException) as e:
            if logger:
                logger.warning(f"WebSocket disconnected: {e} — reconnecting in {reconnect_delay}s")
        except Exception as e:
            if logger:
                logger.error(f"WebSocket error: {e} — reconnecting in {reconnect_delay}s")

        await asyncio.sleep(reconnect_delay)
