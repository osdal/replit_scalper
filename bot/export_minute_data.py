#!/usr/bin/env python3
"""
Download 1-minute candles for every symbol (from config_*.yaml) over a
given period and save each symbol to a CSV file in logs/minutes_1m/.

CSV columns: open_time, open, high, low, close, volume, close_time,
quote_volume, trades, taker_buy_base, taker_buy_quote
(open_time is the UTC timestamp index, ISO format).

Usage:
    python export_minute_data.py --start 2026-07-28 --end 2026-08-28
    python export_minute_data.py --start 2026-07-28 --end 2026-08-28 --symbols BTCUSDT,ETHUSDT
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
    return df


async def main_async(symbols, start, end, out_dir):
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    os.makedirs(out_dir, exist_ok=True)
    try:
        for i, sym in enumerate(symbols, 1):
            try:
                df = await download_symbol(client, sym, start, end)
                out_file = os.path.join(out_dir, f"{sym}.csv")
                df.to_csv(out_file, index=False, encoding="utf-8")
                print(f"[{i}/{len(symbols)}] {sym}: {len(df)} candles -> {out_file}")
            except Exception as e:
                print(f"[{i}/{len(symbols)}] {sym}: ERROR {e}")
    finally:
        await client.close_connection()


def main():
    parser = argparse.ArgumentParser(
        description="Download 1m candles for all symbols and save each to CSV"
    )
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument("--symbols", default=None,
                        help="Comma-separated symbol list (default: all from config_*.yaml)")
    parser.add_argument("--out-dir", default=OUTPUT_DIR, help="Output directory")
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

    print(f"Symbols ({len(symbols)}): {symbols}")
    print(f"Period: {args.start} -> {args.end} (1m)")
    print(f"Output: {args.out_dir}")

    asyncio.run(main_async(symbols, args.start, args.end, args.out_dir))
    print("Done.")


if __name__ == "__main__":
    main()
