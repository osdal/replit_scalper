#!/usr/bin/env python3
"""
Per-preset backtest: run EACH preset in isolation over all symbols for N months on 1h.
Uses each symbol's own config_<sym>.yaml (as live), ATR SL/TP from configs, HTF off.
Data downloaded once per symbol, indicators precomputed once, reused across presets.

Output: logs/backtest_presets_<timestamp>.csv with preset, direction, trades,
winrate, pnl, avg_pnl (per symbol rows) + console totals.
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

from backtester import run_backtest_on_df
from config import load_config
from preset_config import PRESET_CONFIG

load_dotenv()

PRESETS = list(PRESET_CONFIG.keys())


def get_symbol_list():
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    symbols = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            symbols.append(sym)
    return sorted(set(symbols))


async def main_async(symbols, start, end):
    from strategy import calculate_indicators
    from market_data import get_historical_klines

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    silent = logging.getLogger("bt_preset")
    silent.setLevel(logging.CRITICAL)

    # key: preset -> direction -> list of (trades, winrate, pnl, avg)
    results = []
    try:
        for i, sym in enumerate(symbols, 1):
            # Свой конфиг символа (как живой бот)
            fname = sym.replace("USDT", "").lower()
            cfg = load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), f"config_{fname}.yaml"))
            cfg.symbol = sym
            cfg.timeframe = "1h"
            cfg.backtest_start = start
            cfg.backtest_end = end
            cfg.mode = "backtest"
            cfg.htf_enabled = False
            cfg.htf2_enabled = False
            cfg.use_fixed_tp_sl = False

            # Скачиваем 1h данные один раз на символ
            df = await get_historical_klines(client=client, symbol=sym, interval="1h",
                                             start=start, end=end)
            if len(df) < 300:
                print(f"[{i}/{len(symbols)}] {sym}: only {len(df)} candles, skip")
                continue
            df = calculate_indicators(df, cfg)
            df.dropna(inplace=True)

            # Прогон каждого пресета изолированно на одних данных
            for pi, preset in enumerate(PRESETS):
                try:
                    cfg.enabled_presets = [preset]
                    stats = run_backtest_on_df(df, cfg, silent, enabled_presets=[preset], precomputed=True)
                    longs = [t for t in stats.trades if t.direction == "LONG"]
                    shorts = [t for t in stats.trades if t.direction == "SHORT"]
                    for direction, trs in (("LONG", longs), ("SHORT", shorts)):
                        if not trs:
                            continue
                        n = len(trs)
                        wins = sum(1 for t in trs if t.pnl > 0)
                        pnl = sum(t.pnl for t in trs)
                        results.append({
                            "symbol": sym, "preset": preset, "direction": direction,
                            "regime": "all",
                            "trades": n, "winrate": round(wins / n * 100, 1),
                            "pnl": round(pnl, 2), "avg_pnl": round(pnl / n, 3),
                        })
                        # Разбивка по режиму рынка (ADX на входе)
                        buckets = {"trend": lambda t: t.regime_adx >= 25,
                                   "weak": lambda t: 15 <= t.regime_adx < 25,
                                   "flat": lambda t: t.regime_adx < 15}
                        for regime, fn in buckets.items():
                            sub = [t for t in trs if fn(t)]
                            if not sub:
                                continue
                            sn = len(sub)
                            sw = sum(1 for t in sub if t.pnl > 0)
                            sp = sum(t.pnl for t in sub)
                            results.append({
                                "symbol": sym, "preset": preset, "direction": direction,
                                "regime": regime,
                                "trades": sn, "winrate": round(sw / sn * 100, 1),
                                "pnl": round(sp, 2), "avg_pnl": round(sp / sn, 3),
                            })
                except Exception as e:
                    pass  # пресет может не поддерживаться на данных — пропускаем
            print(f"[{i}/{len(symbols)}] {sym}: done ({len(PRESETS)} presets)")
    finally:
        await client.close_connection()
    return results


def main():
    parser = argparse.ArgumentParser(description="Per-preset backtest over months (1h)")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    args = parser.parse_args()
    try:
        datetime.datetime.strptime(args.start, "%Y-%m-%d")
        datetime.datetime.strptime(args.end, "%Y-%m-%d")
    except ValueError:
        sys.exit("Start/end must be YYYY-MM-DD")

    symbols = get_symbol_list()
    print(f"Symbols: {len(symbols)} | Presets: {len(PRESETS)} | "
          f"Period: {args.start} -> {args.end} (1h, per-symbol config, ATR SL/TP, HTF off)")
    print(f"Прогонов: {len(symbols)} символов x {len(PRESETS)} пресетов")

    results = asyncio.run(main_async(symbols, args.start, args.end))

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", f"backtest_presets_{timestamp}.csv")
    if results:
        fieldnames = list(results[0].keys())
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        print(f"\nSaved -> {out_csv}")

    # Сводка по пресетам (по всем символам, split LONG/SHORT) — только строки regime="all"
    agg = defaultdict(lambda: defaultdict(lambda: {"n": 0, "pnl": 0.0, "wins": 0, "wr": 0.0}))
    for r in results:
        if r.get("regime", "all") != "all":
            continue
        p, d = r["preset"], r["direction"]
        a = agg[p][d]
        a["n"] += r["trades"]
        a["pnl"] += r["pnl"]
        a["wins"] += int(r["trades"] * r["winrate"] / 100)
        a["wr"] += r["winrate"] * r["trades"]

    print("\n=== ТОП пресетов по суммарному PnL (LONG+SHORT вместе) ===")
    rows_out = []
    for p in PRESETS:
        total_pnl = sum(agg[p][d]["pnl"] for d in ("LONG", "SHORT"))
        total_n = sum(agg[p][d]["n"] for d in ("LONG", "SHORT"))
        wr = sum(agg[p][d]["wr"] for d in ("LONG", "SHORT")) / max(total_n, 1)
        rows_out.append((p, total_n, wr, total_pnl))
    for p, n, wr, pnl in sorted(rows_out, key=lambda x: -x[3]):
        if n == 0:
            continue
        print(f"  {p:<32} n={n:<5} wr={wr:>5.1f}%  pnl={pnl:>10.2f}")

    print("\n=== Детально LONG / SHORT по каждому пресету ===")
    for p in PRESETS:
        if p not in agg or not agg[p]:
            continue
        line_parts = []
        for d in ("LONG", "SHORT"):
            a = agg[p].get(d)
            if a and a["n"]:
                wr = a["wr"] / a["n"]
                line_parts.append(f"{d}: n={a['n']} wr={wr:.1f}% pnl={a['pnl']:.2f}")
        if line_parts:
            print(f"  {p}: {' | '.join(line_parts)}")

    # Режим-анализ: прибыльность по состоянию рынка (все символы, все пресеты)
    print("\n=== Агрегация по режиму рынка (все пресеты, все символы) ===")
    reg_agg = defaultdict(lambda: {"n": 0, "pnl": 0.0, "wr": 0.0})
    for r in results:
        if r.get("regime", "all") == "all":
            continue
        reg = r["regime"]
        reg_agg[reg]["n"] += r["trades"]
        reg_agg[reg]["pnl"] += r["pnl"]
        reg_agg[reg]["wr"] += r["winrate"] * r["trades"]
    for reg in ("trend", "weak", "flat"):
        a = reg_agg.get(reg)
        if a and a["n"]:
            print(f"  {reg:<6} n={a['n']:<6} wr={a['wr']/a['n']:>5.1f}%  pnl={a['pnl']:>10.2f}")

    print("\n=== Пресет x режим (суммарно LONG+SHORT, топ по PnL в каждом режиме) ===")
    preset_reg = defaultdict(lambda: defaultdict(lambda: {"n": 0, "pnl": 0.0, "wr": 0.0}))
    for r in results:
        if r.get("regime", "all") == "all":
            continue
        pr, reg = r["preset"], r["regime"]
        preset_reg[pr][reg]["n"] += r["trades"]
        preset_reg[pr][reg]["pnl"] += r["pnl"]
        preset_reg[pr][reg]["wr"] += r["winrate"] * r["trades"]
    for reg in ("trend", "weak", "flat"):
        print(f"\n  --- Режим: {reg} ---")
        lines = []
        for pr in PRESETS:
            a = preset_reg[pr].get(reg)
            if a and a["n"] >= 20:  # минимум сделок для значимости
                lines.append((pr, a["n"], a["wr"] / a["n"], a["pnl"]))
        for pr, n, wr, pnl in sorted(lines, key=lambda x: -x[3])[:8]:
            print(f"    {pr:<30} n={n:<4} wr={wr:>5.1f}%  pnl={pnl:>9.2f}")


if __name__ == "__main__":
    main()
