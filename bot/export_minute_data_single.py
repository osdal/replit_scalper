#!/usr/bin/env python3
"""
Download 1-minute candles for every symbol (from config_*.yaml) over a
given period and save ALL symbols into ONE CSV file with a `symbol` column.

CSV columns: symbol, open_time, open, high, low, close, volume, close_time,
quote_volume, trades, taker_buy_base, taker_buy_quote
(open_time is the UTC timestamp, ISO format).

Usage:
    python export_minute_data_single.py --start 2026-07-28 --end 2026-08-28
    python export_minute_data_single.py --start 2026-07-28 --end 2026-08-28 --symbols BTCUSDT,ETHUSDT
"""

import argparse
import asyncio
import os
import sys
from datetime import datetime

import pandas as pd
from binance import AsyncClient
from dotenv import load_dotenv

from config import load_config
from market_data import get_historical_klines

load_dotenv()

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "minutes_1m")


def get_symbol_list():
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    symbols = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            symbols.append(sym)
    return sorted(set(symbols))


async def download_symbol(client, symbol: str, start: str, end: str) -> pd.DataFrame:
    cfg = load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml"))
    df = await get_historical_klines(
        client=client,
        symbol=symbol,
        interval="1m",
        start=start,
        end=end,
    )
    df = df.reset_index()
    df["open_time"] = df["open_time"].dt.strftime("%Y-%m-%d %H:%M:%S")
    df.insert(0, "symbol", symbol)
    return df


async def main_async(symbols, start, end, out_file):
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    os.makedirs(os.path.dirname(out_file), exist_ok=True)
    frames = []
    try:
        for i, sym in enumerate(symbols, 1):
            try:
                df = await download_symbol(client, sym, start, end)
                frames.append(df)
                print(f"[{i}/{len(symbols)}] {sym}: {len(df)} candles")
            except Exception as e:
                print(f"[{i}/{len(symbols)}] {sym}: ERROR {e}")
    finally:
        await client.close_connection()

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        combined.to_csv(out_file, index=False, encoding="utf-8")
        print(f"Saved {len(combined)} rows ({len(frames)} symbols) -> {out_file}")
    else:
        print("No data downloaded, nothing to save.")


def main():
    parser = argparse.ArgumentParser(
        description="Download 1m candles for all symbols and save to ONE CSV"
    )
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated symbol list (default: all from config_*.yaml)")
    parser.add_argument("--out", default=None,
                        help="Output CSV path (default: logs/minutes_1m/minute_all_<start>_<end>.csv)")
    args = parser.parse_args()

    try:
        datetime.strptime(args.start, "%Y-%m-%d")
        datetime.strptime(args.end, "%Y-%m-%d")
    except ValueError:
        sys.exit("Start/end must be YYYY-MM-DD")

    if args.symbols:
        symbols = sorted({s.strip().upper() for s in args.symbols.split(",") if s.strip()})
    else:
        symbols = get_symbol_list()

    if args.out:
        out_file = args.out
    else:
        out_file = os.path.join(OUTPUT_DIR, f"minute_all_{args.start}_{args.end}.csv")

    print(f"Symbols ({len(symbols)}): {symbols}")
    print(f"Period: {args.start} -> {args.end} (1m)")
    print(f"Output: {out_file}")

    asyncio.run(main_async(symbols, args.start, args.end, out_file))
    print("Done.")


if __name__ == "__main__":
    main()
