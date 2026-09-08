#!/usr/bin/env python3
"""
Symbol x Preset x Direction matrix backtest on two periods.
Finds stable profitable links (symbol+preset+direction) that are positive
on BOTH periods (out-of-sample check built in).

Each symbol's config_<sym>.yaml used, 1h, ATR SL/TP, consensus=1 (isolated preset).

Output: logs/symbol_preset_matrix_<ts>.csv
Usage:
    python symbol_preset_matrix.py
"""

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
from strategy import calculate_indicators
from market_data import get_historical_klines

load_dotenv()

PRESETS = list(PRESET_CONFIG.keys())
PERIODS = [("2026-03-01", "2026-05-31", "mar_may"), ("2026-06-01", "2026-09-07", "jun_sep")]


def get_symbol_list():
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    symbols = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            symbols.append(sym)
    return sorted(set(symbols))


async def main():
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    silent = logging.getLogger("sym_preset")
    silent.setLevel(logging.CRITICAL)

    symbols = get_symbol_list()
    print(f"Symbols: {len(symbols)} | Presets: {len(PRESETS)} | Periods: 2")

    # period -> list of rows
    period_rows = {"mar_may": [], "jun_sep": []}
    # key (symbol,preset,direction) -> {period: pnl}
    link_pnl = defaultdict(lambda: {})

    try:
        for si, sym in enumerate(symbols, 1):
            # конфиг символа
            fname = sym.replace("USDT", "").lower()
            cfg = load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)), f"config_{fname}.yaml"))
            cfg.symbol = sym
            cfg.timeframe = "1h"
            cfg.mode = "backtest"
            cfg.htf_enabled = False
            cfg.htf2_enabled = False
            cfg.use_fixed_tp_sl = False
            cfg.min_consensus = 1

            for start, end, pname in PERIODS:
                cfg.backtest_start = start
                cfg.backtest_end = end
                try:
                    df = await get_historical_klines(client=client, symbol=sym, interval="1h",
                                                     start=start, end=end)
                    if len(df) < 300:
                        continue
                    df = calculate_indicators(df, cfg)
                    df.dropna(inplace=True)

                    for preset in PRESETS:
                        cfg.enabled_presets = [preset]
                        try:
                            stats = run_backtest_on_df(df, cfg, silent, enabled_presets=[preset],
                                                       precomputed=True)
                        except Exception:
                            continue
                        for direction in ("LONG", "SHORT"):
                            trs = [t for t in stats.trades if t.direction == direction]
                            if not trs:
                                continue
                            n = len(trs)
                            wins = sum(1 for t in trs if t.pnl > 0)
                            pnl = sum(t.pnl for t in trs)
                            row = {"symbol": sym.replace("USDT", ""), "preset": preset,
                                   "direction": direction, "period": pname,
                                   "trades": n, "winrate": round(wins / n * 100, 1),
                                   "pnl": round(pnl, 2)}
                            period_rows[pname].append(row)
                            link_pnl[(sym.replace("USDT", ""), preset, direction)][pname] = pnl
                except Exception as e:
                    print(f"{sym} {pname}: ERROR {e}")
            print(f"[{si}/{len(symbols)}] {sym}: done")
    finally:
        await client.close_connection()

    # Сохраняем все строки
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs",
                           f"symbol_preset_matrix_{timestamp}.csv")
    all_rows = period_rows["mar_may"] + period_rows["jun_sep"]
    if all_rows:
        fieldnames = list(all_rows[0].keys())
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\nSaved -> {out_csv}")

    # Стабильные связки: плюс на ОБОИХ периодах, мин сделок в каждом
    print("\n=== СТАБИЛЬНО ПЛЮСОВЫЕ СВЯЗКИ (PnL>0 на обоих периодах) ===")
    stable = []
    for (sym, preset, direction), pnls in link_pnl.items():
        if "mar_may" not in pnls or "jun_sep" not in pnls:
            continue
        p1, p2 = pnls["mar_may"], pnls["jun_sep"]
        # минимальное число сделок на период
        def n_for(period):
            for r in period_rows[period]:
                if r["symbol"] == sym and r["preset"] == preset and r["direction"] == direction:
                    return r["trades"], r["winrate"]
            return 0, 0
        n1, _ = n_for("mar_may")
        n2, _ = n_for("jun_sep")
        if p1 > 1 and p2 > 1 and min(n1, n2) >= 5:
            stable.append((sym, preset, direction, n1, n2, p1, p2, p1 + p2))
    stable.sort(key=lambda x: -x[7])
    for s in stable:
        sym, preset, direction, n1, n2, p1, p2, tot = s
        print(f"  {sym:<8} {preset:<26} {direction:<6} n1={n1:<3} n2={n2:<3} "
              f"pnl1={p1:>7.2f} pnl2={p2:>7.2f} total={tot:>8.2f}")

    if not stable:
        print("  (нет связок с PnL>0 на обоих периодах при n>=5)")

    # Худшие стабильно убыточные
    print("\n=== СТАБИЛЬНО УБЫТОЧНЫЕ (PnL<0 оба периода, для отключения) ===")
    bad = []
    for (sym, preset, direction), pnls in link_pnl.items():
        if "mar_may" not in pnls or "jun_sep" not in pnls:
            continue
        p1, p2 = pnls["mar_may"], pnls["jun_sep"]
        def n_for(period):
            for r in period_rows[period]:
                if r["symbol"] == sym and r["preset"] == preset and r["direction"] == direction:
                    return r["trades"]
            return 0
        n1 = n_for("mar_may")
        n2 = n_for("jun_sep")
        if p1 < -3 and p2 < -3 and min(n1, n2) >= 10:
            bad.append((sym, preset, direction, n1, n2, p1, p2, p1 + p2))
    bad.sort(key=lambda x: x[7])
    for s in bad[:20]:
        sym, preset, direction, n1, n2, p1, p2, tot = s
        print(f"  {sym:<8} {preset:<26} {direction:<6} n1={n1:<3} n2={n2:<3} "
              f"pnl1={p1:>7.2f} pnl2={p2:>7.2f} total={tot:>8.2f}")


if __name__ == "__main__":
    asyncio.run(main())
