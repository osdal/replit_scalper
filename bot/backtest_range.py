#!/usr/bin/env python3
"""
C2 — Range trading quick test: покупка у нижней границы диапазона (LONG),
продажа у верхней (SHORT). Диапазон = min(low)/max(high) за N свечей.

Идеи:
- Вход LONG когда low[i] <= lower_bound (цена коснулась низа диапазона)
  и close[i] вернулся внутрь (отскок подтверждён).
- Вход SHORT симметрично у верхней границы.
- Выходы: fixed RR (SL за границей диапазона, TP = NxSL), чтобы сравнить с
  остальными нашими тестами. Плюс вариант "до противоположной границы".

Сетка окон диапазона и множителей RR. Оба периода сразу, большие выборки.
"""

import argparse
import asyncio
import csv
import datetime
import logging
import os
import sys
from collections import defaultdict

import pandas as pd
from binance import AsyncClient
from dotenv import load_dotenv

from market_data import get_historical_klines

load_dotenv()

PERIODS = [("2026-03-01", "2026-05-31", "mar_may"), ("2026-06-01", "2026-09-07", "jun_sep")]
FEE = 0.0005

WINDOWS = [24, 48, 96]       # окно диапазона в 1h свечах
RR_LIST = [1.5, 2.0, 3.0]    # TP = RR * SL


def get_symbol_list():
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    symbols = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            symbols.append(sym)
    return sorted(set(symbols))


def simulate(df, entry_idx, direction, entry, sl, tp):
    sl_pct = abs(entry - sl) / entry * 100 if entry else 0
    if sl_pct <= 0:
        return None
    qty = 0.5 / (entry * sl_pct / 100)
    for i in range(entry_idx + 1, len(df)):
        hi = float(df["high"].iloc[i])
        lo = float(df["low"].iloc[i])
        if direction == "LONG":
            if lo <= sl:
                ep, reason = sl, "SL"
            elif hi >= tp:
                ep, reason = tp, "TP"
            else:
                continue
            pnl = (ep - entry) * qty
        else:
            if hi >= sl:
                ep, reason = sl, "SL"
            elif lo <= tp:
                ep, reason = tp, "TP"
            else:
                continue
            pnl = (entry - ep) * qty
        comm = (abs(entry) + abs(ep)) * qty * FEE
        return pnl - comm, reason, ep, df.index[i]
    return None


async def run(symbols):
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    all_rows = []
    try:
        for si, sym in enumerate(symbols, 1):
            for start, end, pname in PERIODS:
                try:
                    df = await get_historical_klines(client=client, symbol=sym, interval="1h", start=start, end=end)
                    if len(df) < 300:
                        continue
                    df = df.reset_index(drop=True)
                    n = len(df)
                    for win in WINDOWS:
                        # Скользящие границы диапазона
                        hi_roll = df["high"].rolling(win).max().shift(1)  # верх до текущей свечи
                        lo_roll = df["low"].rolling(win).min().shift(1)   # низ до текущей свечи
                        for rr in RR_LIST:
                            tag = f"win{win}_rr{rr}"
                            i = win + 1
                            while i < n:
                                o = float(df["open"].iloc[i])
                                h = float(df["high"].iloc[i])
                                l = float(df["low"].iloc[i])
                                c = float(df["close"].iloc[i])
                                hi_b = float(hi_roll.iloc[i])
                                lo_b = float(lo_roll.iloc[i])
                                if not (hi_b == hi_b and lo_b == lo_b):
                                    i += 1
                                    continue
                                direction = None
                                entry = None
                                sl = None
                                # LONG: цена коснулась низа и закрылась выше (отскок)
                                if l <= lo_b and c > lo_b:
                                    direction = "LONG"
                                    entry = c
                                    sl = lo_b * 0.999
                                # SHORT: коснулась верха и закрылась ниже
                                elif h >= hi_b and c < hi_b:
                                    direction = "SHORT"
                                    entry = c
                                    sl = hi_b * 1.001
                                if direction is None:
                                    i += 1
                                    continue
                                tp = (entry + rr * (entry - sl)) if direction == "LONG" else (entry - rr * (sl - entry))
                                res = simulate(df, i, direction, entry, sl, tp)
                                if res:
                                    pnl, reason, ep, t_exit = res
                                    all_rows.append({
                                        "symbol": sym.replace("USDT", ""), "period": f"{start}_{end}",
                                        "param_tag": tag, "direction": direction,
                                        "pnl": round(pnl, 4), "exit_reason": reason,
                                    })
                                i += 2  # шаг 2 свечи, чтобы не входить каждый бар подряд
                except Exception as e:
                    print(f"{sym} {pname}: ERR {e}")
            print(f"[{si}/{len(symbols)}] {sym}: done")
    finally:
        await client.close_connection()
    return all_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=None)
    args = parser.parse_args()
    symbols = get_symbol_list()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print(f"Symbols: {len(symbols)} | windows={WINDOWS} rr={RR_LIST} | 2 periods")

    rows = asyncio.run(run(symbols))
    if not rows:
        print("Нет сделок")
        return
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", f"range_{ts}.csv")
    fn = list(rows[0].keys())
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fn)
        w.writeheader()
        w.writerows(rows)
    print(f"\nSaved -> {out} ({len(rows)} сделок)")

    by_tag = defaultdict(list)
    for r in rows:
        by_tag[r["param_tag"]].append(r)
    print("\n=== Сводка по комбо ===")
    for tag in sorted(by_tag, key=lambda t: -sum(float(x["pnl"]) for x in by_tag[t])):
        items = by_tag[tag]
        n = len(items)
        wins = sum(1 for x in items if float(x["pnl"]) > 0)
        pnl = sum(float(x["pnl"]) for x in items)
        print(f"  {tag:<12} n={n:<6} wr={wins/n*100:>5.1f}%  pnl={pnl:>9.2f}")


if __name__ == "__main__":
    main()
