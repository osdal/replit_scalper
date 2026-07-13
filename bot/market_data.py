import asyncio
import json
import logging
from typing import Callable, Dict, Optional

import pandas as pd
from binance import AsyncClient


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
    shutdown_event: Optional[asyncio.Event] = None,
) -> None:
    """
    REST polling — checks for new closed candles every poll_seconds.
    При старте инициализирует last_seen последней закрытой свечой,
    чтобы не прокручивать старые свечи после перезапуска.
    
    shutdown_event: если передан, polling выходит когда событие установлено.
    """
    last_seen: Dict[str, pd.Timestamp] = {}

    # Инициализируем last_seen до начала поллинга —
    # берём последнюю закрытую свечу по каждому интервалу
    for interval in handlers:
        try:
            klines = await client.futures_klines(
                symbol=symbol, interval=interval, limit=2
            )
            df = _klines_to_df(klines)
            last_seen[interval] = df.iloc[-2].name
            if logger:
                logger.info(
                    f"Polling init | {interval} last_seen={last_seen[interval]}"
                )
        except Exception as e:
            if logger:
                logger.error(f"Polling init error ({interval}): {e}")

    if logger:
        logger.info(
            f"Polling started | intervals={list(handlers.keys())} "
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
                klines = await client.futures_klines(
                    symbol=symbol, interval=interval, limit=2
                )
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