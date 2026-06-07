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
        "open":  float(kline["o"]),
        "high":  float(kline["h"]),
        "low":   float(kline["l"]),
        "close": float(kline["c"]),
        "volume": float(kline["v"]),
    })


async def start_kline_polling(
    client: AsyncClient,
    symbol: str,
    handlers: Dict[str, Callable],
    logger: Optional[logging.Logger] = None,
    poll_seconds: int = 10,
) -> None:
    """
    REST polling fallback — checks for new closed candles every poll_seconds.
    handlers: { "5m": on_5m_candle_close, "1h": on_1h_candle_close }
    Works reliably on all platforms without WebSocket issues.
    """
    last_seen: Dict[str, pd.Timestamp] = {}

    if logger:
        logger.info(
            f"Polling started | intervals={list(handlers.keys())} "
            f"every {poll_seconds}s"
        )

    while True:
        for interval, callback in handlers.items():
            try:
                klines = await client.futures_klines(
                    symbol=symbol, interval=interval, limit=2
                )
                df = _klines_to_df(klines)
                closed = df.iloc[-2]
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
                        "open":  closed["open"],
                        "high":  closed["high"],
                        "low":   closed["low"],
                        "close": closed["close"],
                        "volume": closed["volume"],
                    })
                    result = callback(candle)
                    if asyncio.iscoroutine(result):
                        await result

            except Exception as e:
                if logger:
                    logger.error(f"Polling error ({interval}): {e}")

        await asyncio.sleep(poll_seconds)


async def start_kline_socket(
    client: AsyncClient,
    symbol: str,
    interval: str,
    on_candle_close: Callable,
    logger: Optional[logging.Logger] = None,
    reconnect_delay: int = 5,
) -> None:
    """Single-stream WebSocket."""
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
