#!/usr/bin/env python3
"""
Hybrid backtest: 1h signal (as live), but entry happens earlier — on the first 15m
candle of the next hour that confirms the direction (price moves in signal's side).
Filters cancelled signals (price reversed within the first M15 candles).

Compares vs baseline 1h-close entry.
Output: logs/backtest_hybrid_<ts>.csv (per-symbol LONG/SHORT totals).

Usage:
    python backtest_hybrid.py --start 2026-06-01 --end 2026-09-07
    optional: --consensus 2 --max-adx 25 --tp-mult 2.0 --confirm-m15 1
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

from config import load_config
from preset_config import PRESET_CONFIG, get_enabled_presets
from strategy import calculate_indicators, get_all_signals

load_dotenv()


def get_symbol_list():
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    symbols = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            symbols.append(sym)
    return sorted(set(symbols))


async def download(client, symbol, interval, start, end):
    from market_data import get_historical_klines
    return await get_historical_klines(client=client, symbol=symbol, interval=interval,
                                       start=start, end=end)


def get_signal_at(df_1h, idx, cfg, htf_none):
    """Возвращает сигнал на закрытии 1h-свечи df_1h.iloc[idx] (согласованный)."""
    window = df_1h.iloc[: idx + 1]
    signals = get_all_signals(window, cfg, None, cfg.enabled_presets,
                              min_consensus=getattr(cfg, "min_consensus", 1))
    if not signals:
        return None
    signals.sort(key=lambda s: s.volume, reverse=True)
    return signals[0]


def adx_at(df, idx):
    if "adx" not in df.columns:
        return 0.0
    try:
        v = float(df.iloc[idx].get("adx", 0) or 0)
        return v
    except Exception:
        return 0.0


def simulate_position(df_15, start_i, signal, cfg, rr):
    """
    Симулирует позицию, открытую по сигналу, начиная с M15-свечи start_i.
    Возвращает (pnl_net, exit_reason, closed_price, exit_i, entry_price) или None,
    если до конца данных не закрылась (считаем по последней цене как незакрытую).
    """
    entry = float(df_15.iloc[start_i]["close"])
    atr_abs = float(signal.atr or 0)
    # Стопы от ATR сигнала (1h), RR из параметра
    if entry <= 0 or atr_abs <= 0:
        return None
    sl_pct = 1.5 * (atr_abs / entry) * 100
    tp_pct = rr * sl_pct
    direction = signal.direction
    if direction == "LONG":
        sl_price = entry - entry * sl_pct / 100
        tp1_price = entry + entry * tp_pct / 100
    else:
        sl_price = entry + entry * sl_pct / 100
        tp1_price = entry - entry * tp_pct / 100

    fee = getattr(cfg, "commission_pct", 0.0) / 100.0
    qty = getattr(cfg, "fixed_risk_usd", 0.5) / (entry * sl_pct / 100) if sl_pct > 0 else 0
    if qty <= 0:
        return None

    for i in range(start_i, len(df_15)):
        row = df_15.iloc[i]
        high, low = float(row["high"]), float(row["low"])
        if direction == "LONG":
            if low <= sl_price:
                exit_px = sl_price
                pnl = (exit_px - entry) * qty
                comm = (entry + exit_px) * qty * fee
                return (pnl - comm, "SL", exit_px, i, entry)
            if high >= tp1_price:
                exit_px = tp1_price
                pnl = (exit_px - entry) * qty
                comm = (entry + exit_px) * qty * fee
                return (pnl - comm, "TP1", exit_px, i, entry)
        else:
            if high >= sl_price:
                exit_px = sl_price
                pnl = (entry - exit_px) * qty
                comm = (entry + exit_px) * qty * fee
                return (pnl - comm, "SL", exit_px, i, entry)
            if low <= tp1_price:
                exit_px = tp1_price
                pnl = (entry - exit_px) * qty
                comm = (entry + exit_px) * qty * fee
                return (pnl - comm, "TP1", exit_px, i, entry)
    return None  # не закрылась до конца данных


async def run_symbol(sym, cfg, df_1h, df_15, consensus, max_adx, rr, confirm_m15, silent):
    """
    Основной гибридный цикл по 1h свечам: сигнал на закрытии 1h -> смотрим первые
    confirm_m15 свечей нового часа на 15m -> если движение в сторону сигнала, вход.
    """
    trades = []
    cfg.min_consensus = consensus
    cfg.enabled_presets = list(PRESET_CONFIG.keys())

    i = 0
    n1 = len(df_1h)
    while i < n1 - 1:
        # Пропускаем прогрев: нужно достаточно истории для индикаторов
        if i < 200:
            i += 1
            continue

        signal = get_signal_at(df_1h, i, cfg, None)
        if signal is None:
            i += 1
            continue

        if max_adx and max_adx > 0:
            if adx_at(df_1h, i) > max_adx:
                i += 1
                continue

        # Время закрытия 1h-свечи (индекс i) -> ищем M15 свечи строго после
        t_close = df_1h.index[i]
        # Находим первую M15 свечу с временем > t_close
        m_start = df_15.index.searchsorted(t_close, side="right")
        if m_start >= len(df_15):
            break

        # Проверяем подтверждение на первых confirm_m15 свечах нового часа
        confirmed_idx = None
        entry_side_price = None
        for k in range(confirm_m15):
            mi = m_start + k
            if mi >= len(df_15):
                break
            close_px = float(df_15.iloc[mi]["close"])
            # сигнальная цена = закрытие 1h
            sig_px = float(df_1h.iloc[i]["close"])
            if signal.direction == "LONG" and close_px > sig_px * 1.0001:
                confirmed_idx = mi
                break
            if signal.direction == "SHORT" and close_px < sig_px * 0.9999:
                confirmed_idx = mi
                break

        if confirmed_idx is None:
            # Сигнал не подтверждён/отменён в первые M15 — пропуск (фильтр отмены)
            i += 1
            continue

        res = simulate_position(df_15, confirmed_idx, signal, cfg, rr)
        if res is not None:
            pnl, reason, exit_px, exit_i, entry = res
            trades.append({
                "symbol": sym, "direction": signal.direction,
                "preset": signal.preset,
                "trades": 1,
                "winrate": 100.0 if pnl > 0 else 0.0,
                "pnl": round(pnl, 4),
            })
            # После закрытия позиции продолжаем с 1h свечи, следующей за временем выхода
            t_exit = df_15.index[exit_i]
            i = max(i + 1, df_1h.index.searchsorted(t_exit, side="right") - 1)
        else:
            # Не закрылась — пропускаем (край данных), выходим
            break

    return trades


async def main_async(symbols, start, end, consensus, max_adx, rr, confirm_m15):
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    silent = logging.getLogger("hybrid")
    silent.setLevel(logging.CRITICAL)

    results = []
    try:
        for si, sym in enumerate(symbols, 1):
            try:
                fname = sym.replace("USDT", "").lower()
                cfg = load_config(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                               f"config_{fname}.yaml"))
                cfg.symbol = sym
                cfg.timeframe = "1h"
                cfg.backtest_start = start
                cfg.backtest_end = end
                cfg.mode = "backtest"
                cfg.htf_enabled = False
                cfg.htf2_enabled = False
                cfg.use_fixed_tp_sl = False

                df_1h = await download(client, sym, "1h", start, end)
                df_15 = await download(client, sym, "15m", start, end)
                if len(df_1h) < 300 or len(df_15) < 300:
                    print(f"[{si}/{len(symbols)}] {sym}: мало данных, skip")
                    continue
                df_1h = calculate_indicators(df_1h, cfg)
                df_1h.dropna(inplace=True)
                df_15 = calculate_indicators(df_15, cfg)
                df_15.dropna(inplace=True)

                trades = await run_symbol(sym, cfg, df_1h, df_15, consensus, max_adx, rr,
                                          confirm_m15, silent)
                # Агрегируем по направлению
                agg = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0})
                for t in trades:
                    a = agg[t["direction"]]
                    a["n"] += 1
                    a["pnl"] += t["pnl"]
                    if t["pnl"] > 0:
                        a["w"] += 1
                for d in ("LONG", "SHORT"):
                    a = agg.get(d)
                    if a and a["n"]:
                        results.append({
                            "symbol": sym, "direction": d, "trades": a["n"],
                            "winrate": round(a["w"] / a["n"] * 100, 1),
                            "pnl": round(a["pnl"], 2),
                        })
                tot = sum(a["n"] for a in agg.values())
                print(f"[{si}/{len(symbols)}] {sym}: trades={tot}")
            except Exception as e:
                print(f"[{si}/{len(symbols)}] {sym}: ERROR {e}")
    finally:
        await client.close_connection()
    return results


def main():
    parser = argparse.ArgumentParser(description="Hybrid 1h-signal + M15-confirm backtest")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--consensus", type=int, default=1)
    parser.add_argument("--max-adx", type=float, default=None)
    parser.add_argument("--tp-mult", type=float, default=2.0, help="RR: TP = mult*SL")
    parser.add_argument("--confirm-m15", type=int, default=1,
                        help="Сколько первых M15 свечей нового часа проверять на подтверждение")
    args = parser.parse_args()
    try:
        datetime.datetime.strptime(args.start, "%Y-%m-%d")
        datetime.datetime.strptime(args.end, "%Y-%m-%d")
    except ValueError:
        sys.exit("Start/end must be YYYY-MM-DD")

    symbols = get_symbol_list()
    print(f"Symbols: {len(symbols)} | Period: {args.start}->{args.end}")
    print(f"Hybrid: 1h signal (consensus={args.consensus}, max_adx={args.max_adx}) + "
          f"M15 confirm ({args.confirm_m15} candles), RR {args.tp_mult}")
    print("=" * 80)

    results = asyncio.run(main_async(symbols, args.start, args.end,
                                     args.consensus, args.max_adx, args.tp_mult,
                                     args.confirm_m15))

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs",
                           f"backtest_hybrid_{timestamp}.csv")
    if results:
        fieldnames = list(results[0].keys())
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(results)
        print(f"\nSaved -> {out_csv}")

        agg = defaultdict(lambda: {"n": 0, "pnl": 0.0, "w": 0})
        for r in results:
            a = agg[r["direction"]]
            a["n"] += r["trades"]
            a["pnl"] += r["pnl"]
            a["w"] += int(r["trades"] * r["winrate"] / 100)
        print("\n=== ИТОГ HYBRID ===")
        for d in ("LONG", "SHORT"):
            a = agg[d]
            if a["n"]:
                print(f"  {d}: n={a['n']} wr={a['w']/a['n']*100:.1f}% pnl={a['pnl']:.2f}")
        tot_n = sum(a["n"] for a in agg.values())
        tot_pnl = sum(a["pnl"] for a in agg.values())
        tot_w = sum(a["w"] for a in agg.values())
        print(f"  ВСЕГО: n={tot_n} wr={tot_w/tot_n*100:.1f}% pnl={tot_pnl:.2f}")


if __name__ == "__main__":
    main()
