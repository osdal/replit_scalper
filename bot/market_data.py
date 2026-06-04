import asyncio
from datetime import datetime
from typing import Callable, Optional

import pandas as pd
from binance import AsyncClient, BinanceSocketManager
from binance.enums import HistoricalKlinesType


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
        klines_type=HistoricalKlinesType.FUTURES,
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


async def start_kline_socket(
    client: AsyncClient,
    symbol: str,
    interval: str,
    on_candle_close: Callable[[pd.Series], None],
) -> None:
    bm = BinanceSocketManager(client)
    async with bm.futures_kline_socket(symbol=symbol, interval=interval) as stream:
        while True:
            msg = await stream.recv()
            if msg is None:
                continue
            kline = msg.get("k", {})
            if not kline.get("x"):
                continue
            candle = pd.Series({
                "open_time": pd.to_datetime(kline["t"], unit="ms"),
                "open": float(kline["o"]),
                "high": float(kline["h"]),
                "low": float(kline["l"]),
                "close": float(kline["c"]),
                "volume": float(kline["v"]),
            })
            on_candle_close(candle)
