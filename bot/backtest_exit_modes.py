#!/usr/bin/env python3
"""
Compare EXIT strategies on the SAME Pin Bar entries (best combo).
Вопрос: проблема во входе или в выходе?

Одинаковые входы Pin Bar (r2.5_u0.3_lb3), разные выходы:
  fixed   — TP = 2R (контроль, текущий)
  ladder  — лесенка: 1/3 на +1R, 1/3 на +2R, 1/3 на +3R (SL->breakeven после 1-й, +1R после 2-й)
  time    — фикс. TP=2R, но если через 24 свечи не в плюсе — выход по рынку
  lad_time — лесенка + выход по времени (24 свечи, если не закрыт — по рынку)
  wide    — TP = 3R (для сравнения с лесенкой)

Данные скачиваются один раз, все выходы считаются на тех же входах.
"""

import asyncio
import csv
import datetime
import logging
import os
import sys
from collections import defaultdict

from binance import AsyncClient
from dotenv import load_dotenv

from backtest_pinbar import get_symbol_list, prepare, detect_pin, FEE, PERIODS

load_dotenv()

# Используем лучшую комбо из сетки
RATIO, URATIO, LOOKBACK = 2.5, 0.3, 3
EXITS = ["fixed", "ladder", "time", "lad_time", "wide"]


def simulate_exit(df, entry_idx, direction, entry, sl, mode, max_hold=24):
    """
    Симулирует один вход с заданным режимом выхода.
    entry_idx — свеча входа (закрыта), следим со следующей.
    sl — уровень стопа (для LONG ниже entry).
    """
    # расстояние риска R
    if direction == "LONG":
        r_dist = entry - sl
    else:
        r_dist = sl - entry
    if r_dist <= 0:
        return None
    sl_pct = r_dist / entry * 100
    qty_total = 0.5 / (entry * sl_pct / 100)  # весь объём
    if qty_total <= 0:
        return None

    # доли для лесенки
    third = qty_total / 3.0

    # состояние
    remaining = qty_total
    realized = 0.0
    cur_sl = sl
    rung_closed = [False, False, False]  # 1R, 2R, 3R
    last_close_px = entry
    exits = []

    def price_for(mult):
        return entry + mult * r_dist if direction == "LONG" else entry - mult * r_dist

    def close_part(px, qty):
        nonlocal realized, remaining
        if qty <= 0 or remaining <= 1e-12:
            return
        qty = min(qty, remaining)
        if direction == "LONG":
            pnl = (px - entry) * qty
        else:
            pnl = (entry - px) * qty
        comm = (abs(entry) + abs(px)) * qty * FEE
        realized += pnl - comm
        remaining -= qty
        exits.append((px, qty))

    for i in range(entry_idx + 1, len(df)):
        hi = float(df["high"].iloc[i])
        lo = float(df["low"].iloc[i])
        cl = float(df["close"].iloc[i])
        last_close_px = cl
        held = i - entry_idx

        if direction == "LONG":
            # стоп
            if lo <= cur_sl and remaining > 1e-12:
                close_part(cur_sl, remaining)
                break
            # лесенка
            if mode in ("ladder", "lad_time"):
                if not rung_closed[0] and hi >= price_for(1.0):
                    close_part(price_for(1.0), third)
                    rung_closed[0] = True
                    cur_sl = entry  # breakeven для остатка
                if not rung_closed[1] and hi >= price_for(2.0) and remaining > 1e-12:
                    close_part(price_for(2.0), third)
                    rung_closed[1] = True
                    cur_sl = entry + 1.0 * r_dist  # lock +1R
                if not rung_closed[2] and hi >= price_for(3.0) and remaining > 1e-12:
                    close_part(price_for(3.0), remaining)
                    rung_closed[2] = True
                    break
                if remaining <= 1e-12:
                    break
            # фикс. TP
            if mode in ("fixed", "time"):
                if hi >= price_for(2.0):
                    close_part(price_for(2.0), remaining)
                    break
                if mode == "time" and held >= max_hold:
                    # если через max_hold не в плюсе (ниже entry) — выходим
                    if cl < entry:
                        close_part(cl, remaining)
                        break
            if mode == "wide" and hi >= price_for(3.0):
                close_part(price_for(3.0), remaining)
                break
        else:  # SHORT
            if hi >= cur_sl and remaining > 1e-12:
                close_part(cur_sl, remaining)
                break
            if mode in ("ladder", "lad_time"):
                if not rung_closed[0] and lo <= price_for(1.0):
                    close_part(price_for(1.0), third)
                    rung_closed[0] = True
                    cur_sl = entry
                if not rung_closed[1] and lo <= price_for(2.0) and remaining > 1e-12:
                    close_part(price_for(2.0), third)
                    rung_closed[1] = True
                    cur_sl = entry - 1.0 * r_dist
                if not rung_closed[2] and lo <= price_for(3.0) and remaining > 1e-12:
                    close_part(price_for(3.0), remaining)
                    rung_closed[2] = True
                    break
                if remaining <= 1e-12:
                    break
            if mode in ("fixed", "time"):
                if lo <= price_for(2.0):
                    close_part(price_for(2.0), remaining)
                    break
                if mode == "time" and held >= max_hold:
                    if cl > entry:
                        close_part(cl, remaining)
                        break
            if mode == "wide" and lo <= price_for(3.0):
                close_part(price_for(3.0), remaining)
                break

        # lad_time: через max_hold если остаток и не в плюсе — выходим по рынку
        if mode == "lad_time" and held >= max_hold and remaining > 1e-12:
            close_part(last_close_px, remaining)
            break

    if remaining > 1e-12:
        # не закрылась до конца данных — закрываем по последней цене
        close_part(last_close_px, remaining)

    return realized, "mixed", last_close_px, qty_total


async def main():
    parser = __import__("argparse").ArgumentParser()
    parser.add_argument("--symbols", default=None)
    args = parser.parse_args()
    symbols = get_symbol_list()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    print(f"Symbols: {len(symbols)} | PinBar r{RATIO}_u{URATIO}_lb{LOOKBACK} | exits: {EXITS}")

    all_rows = []
    try:
        for si, sym in enumerate(symbols, 1):
            for start, end, pname in PERIODS:
                try:
                    from market_data import get_historical_klines
                    df = await get_historical_klines(client=client, symbol=sym, interval="1h", start=start, end=end)
                    if len(df) < 300:
                        continue
                    df = prepare(df)
                    df.dropna(inplace=True)
                    n = len(df)
                    i = LOOKBACK
                    while i < n - 1:
                        pin = detect_pin(df, i, RATIO, URATIO, LOOKBACK)
                        if pin is None:
                            i += 1
                            continue
                        nxt = i + 1
                        c_nxt = float(df["close"].iloc[nxt])
                        hi_pin = float(df["high"].iloc[i])
                        lo_pin = float(df["low"].iloc[i])
                        if pin == "BULL" and c_nxt <= hi_pin:
                            i += 1
                            continue
                        if pin == "BEAR" and c_nxt >= lo_pin:
                            i += 1
                            continue
                        entry = c_nxt
                        direction = "LONG" if pin == "BULL" else "SHORT"
                        sl = lo_pin if direction == "LONG" else hi_pin

                        for mode in EXITS:
                            res = simulate_exit(df, nxt, direction, entry, sl, mode)
                            if res is None:
                                continue
                            pnl, _, _, qty = res
                            all_rows.append({
                                "symbol": sym.replace("USDT", ""),
                                "period": f"{start}_{end}",
                                "exit_mode": mode,
                                "direction": direction,
                                "pnl": round(pnl, 4),
                                "entry_time": str(df.index[nxt]),
                            })
                        i = nxt + 1
                except Exception as e:
                    print(f"{sym} {pname}: ERR {e}")
            print(f"[{si}/{len(symbols)}] {sym}: done")
    finally:
        await client.close_connection()

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", f"exit_modes_{ts}.csv")
    if all_rows:
        fn = list(all_rows[0].keys())
        with open(out, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fn)
            w.writeheader()
            w.writerows(all_rows)
        print(f"\nSaved -> {out}")

    # Сводка
    by_mode = defaultdict(list)
    for r in all_rows:
        by_mode[r["exit_mode"]].append(r)
    print("\n=== Сравнение режимов выхода (одинаковые входы Pin Bar) ===")
    for mode in EXITS:
        items = by_mode.get(mode, [])
        n = len(items)
        if not n:
            continue
        wins = sum(1 for x in items if float(x["pnl"]) > 0)
        pnl = sum(float(x["pnl"]) for x in items)
        print(f"  {mode:<9} n={n:<5} winrate={wins/n*100:>5.1f}%  pnl={pnl:>9.2f}")
    # по периодам
    print("\n=== По периодам x режим ===")
    for mode in EXITS:
        items = by_mode.get(mode, [])
        bp = defaultdict(list)
        for x in items:
            bp[x["period"]].append(x)
        parts = []
        for p in sorted(bp):
            sub = bp[p]
            n = len(sub)
            w = sum(1 for x in sub if float(x["pnl"]) > 0)
            pnl = sum(float(x["pnl"]) for x in sub)
            parts.append(f"{p.split('_')[0]}:wr{w/n*100:.0f}%:{pnl:.0f}")
        if parts:
            print(f"  {mode:<9} {' | '.join(parts)}")


if __name__ == "__main__":
    asyncio.run(main())
