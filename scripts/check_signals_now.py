import asyncio
import os
import pandas as pd
from binance import AsyncClient
from dotenv import load_dotenv

load_dotenv()

async def get_recent_klines(client, symbol, interval, limit=200):
    klines = await client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    columns = [
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ]
    df = pd.DataFrame(klines, columns=columns)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    for col in ["open", "high", "low", "close", "volume", "quote_volume"]:
        df[col] = df[col].astype(float)
    df.set_index("open_time", inplace=True)
    return df

def calculate_indicators(df, ema_fast=10, ema_slow=24, volume_ma_period=20, adx_threshold=20.0, adx_period=14):
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=ema_fast, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=ema_slow, adjust=False).mean()
    df["volume_ma"] = df["volume"].rolling(window=volume_ma_period).mean()
    if adx_threshold > 0:
        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - df["close"].shift(1)).abs()
        tr3 = (df["low"] - df["close"].shift(1)).abs()
        df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        df["atr"] = df["tr"].ewm(alpha=1 / adx_period, adjust=False).mean()
        up_move = df["high"] - df["high"].shift(1)
        down_move = df["low"].shift(1) - df["low"]
        df["+dm"] = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        df["-dm"] = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        df["+dm_s"] = df["+dm"].ewm(alpha=1 / adx_period, adjust=False).mean()
        df["-dm_s"] = df["-dm"].ewm(alpha=1 / adx_period, adjust=False).mean()
        df["+di"] = 100 * df["+dm_s"] / df["atr"]
        df["-di"] = 100 * df["-dm_s"] / df["atr"]
        df["dx"] = 100 * (df["+di"] - df["-di"]).abs() / (df["+di"] + df["-di"])
        df["adx"] = df["dx"].ewm(alpha=1 / adx_period, adjust=False).mean()
    return df

def check_signal(df, ema_fast=10, ema_slow=24, volume_multiplier=1.7, volume_ma_period=20, adx_threshold=20.0, adx_period=14):
    if len(df) < max(ema_slow, volume_ma_period, adx_period if adx_threshold > 0 else 0) + 1:
        return "NOT_ENOUGH_DATA"
    cur = df.iloc[-1]
    prev = df.iloc[-2]
    bullish = prev["ema_fast"] <= prev["ema_slow"] and cur["ema_fast"] > cur["ema_slow"]
    bearish = prev["ema_fast"] >= prev["ema_slow"] and cur["ema_fast"] < cur["ema_slow"]
    if not bullish and not bearish:
        return "NO_EMA_CROSS"
    if cur["volume"] < cur["volume_ma"] * volume_multiplier:
        return f"LOW_VOLUME ({cur['volume']:.2f} < {cur['volume_ma'] * volume_multiplier:.2f})"
    if adx_threshold > 0:
        adx = cur["adx"]
        if pd.isna(adx) or adx < adx_threshold:
            return f"LOW_ADX ({adx:.2f} < {adx_threshold})"
    direction = "LONG" if bullish else "SHORT"
    return f"SIGNAL_{direction}"

async def main():
    c = await AsyncClient.create(
        os.getenv("BINANCE_API_KEY", ""),
        os.getenv("BINANCE_API_SECRET", ""),
    )
    for sym in ["SOLUSDT", "ETHUSDT", "BTCUSDT", "POLUSDT"]:
        try:
            df = await get_recent_klines(c, sym, "5m", 200)
            df = calculate_indicators(df, ema_fast=10, ema_slow=24, adx_threshold=20.0)
            result = check_signal(df, ema_fast=10, ema_slow=24, volume_multiplier=1.7, adx_threshold=20.0)
            cur = df.iloc[-1]
            print(f"{sym}: {result} | close={cur['close']:.4f} ema_fast={cur['ema_fast']:.4f} ema_slow={cur['ema_slow']:.4f} volume={cur['volume']:.2f} volume_ma={cur['volume_ma']:.2f} adx={cur.get('adx', float('nan')):.2f}")
        except Exception as e:
            print(f"{sym}: ERROR {e}")
    await c.close_connection()

asyncio.run(main())
