#!/usr/bin/env python3
"""
Backtest all enabled presets over N months on 1h and split results by direction
(LONG vs SHORT) to answer: is SHORT systematically unprofitable or was it one week?

Uses the SAME logic as live bots: config.yaml template overridden to each symbol,
timeframe=1h, ATR-based SL/TP (use_fixed_tp_sl=False), HTF disabled (as in configs),
one position at a time per symbol.

Output: console summary + logs/backtest_direction_<timestamp>.csv
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


def get_symbol_list():
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    symbols = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            symbols.append(sym)
    return sorted(set(symbols))


async def download_klines(client, symbol: str, start: str, end: str, timeframe: str = "1h") -> pd.DataFrame:
    from market_data import get_historical_klines
    return await get_historical_klines(
        client=client,
        symbol=symbol,
        interval=timeframe,
        start=start,
        end=end,
    )


async def main_async(symbols, start, end, tp_mult=None, tp_mult_long=None, tp_mult_short=None,
                     consensus=1, min_adx=None, max_adx=None,
                     tp1_close_pct=None, tp2_mult=None, timeframe="1h",
                     consensus_flat=None, consensus_weak=None, consensus_trend=None,
                     block_short_trend=False, block_hours=None, exclude_presets=None):
    from strategy import calculate_indicators

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    silent = logging.getLogger("bt_direction")
    silent.setLevel(logging.CRITICAL)

    all_rows = []
    per_symbol = {}
    try:
        for i, sym in enumerate(symbols, 1):
            try:
                # Каждый символ использует СВОЙ config_<sym>.yaml — ровно как живой бот.
                fname = sym.replace("USDT", "").lower()
                cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"config_{fname}.yaml")
                cfg = load_config(cfg_path)
                cfg.symbol = sym
                cfg.timeframe = timeframe
                cfg.backtest_start = start
                cfg.backtest_end = end
                cfg.mode = "backtest"
                # HTF у живых ботов выключен (htf_enabled: false в конфигах)
                cfg.htf_enabled = False
                cfg.htf2_enabled = False
                # ATR-based SL/TP как в live (use_fixed_tp_sl: false в конфигах)
                cfg.use_fixed_tp_sl = False
                if tp_mult is not None:
                    cfg.atr_tp_multiplier = tp_mult
                if tp_mult_long is not None:
                    cfg.atr_tp_multiplier_long = tp_mult_long
                if tp_mult_short is not None:
                    cfg.atr_tp_multiplier_short = tp_mult_short
                cfg.min_consensus = consensus
                if min_adx is not None:
                    cfg.min_adx_filter = min_adx
                if max_adx is not None:
                    cfg.max_adx_filter = max_adx
                if tp1_close_pct is not None:
                    cfg.tp1_close_pct = tp1_close_pct
                if tp2_mult is not None:
                    cfg.atr_tp2_multiplier = tp2_mult
                if consensus_flat is not None:
                    cfg.consensus_flat = consensus_flat
                if consensus_weak is not None:
                    cfg.consensus_weak = consensus_weak
                if consensus_trend is not None:
                    cfg.consensus_trend = consensus_trend
                cfg.trend_block_short = block_short_trend
                if block_hours is not None:
                    cfg.block_hours_utc = [int(h) for h in block_hours.split(",") if h.strip()]
                if exclude_presets is not None:
                    cfg.excluded_presets = [p.strip() for p in exclude_presets.split(",") if p.strip()]
                cfg.enabled_presets = list(PRESET_CONFIG.keys())

                df = await download_klines(client, sym, start, end, timeframe)
                if len(df) < 300:
                    print(f"[{i}/{len(symbols)}] {sym}: only {len(df)} candles, skip")
                    continue
                df = calculate_indicators(df, cfg)
                df.dropna(inplace=True)
                stats = run_backtest_on_df(df, cfg, silent, enabled_presets=cfg.enabled_presets, precomputed=True)

                long_trades = [t for t in stats.trades if t.direction == "LONG"]
                short_trades = [t for t in stats.trades if t.direction == "SHORT"]
                per_symbol[sym] = {
                    "LONG": long_trades, "SHORT": short_trades, "ALL": stats.trades
                }
                print(f"[{i}/{len(symbols)}] {sym}: trades={len(stats.trades)} "
                      f"(LONG={len(long_trades)} SHORT={len(short_trades)})")
            except Exception as e:
                print(f"[{i}/{len(symbols)}] {sym}: ERROR {e}")
    finally:
        await client.close_connection()
    return per_symbol


def _trade_rows(per_symbol):
    """Разворачивает все сделки в строки для CSV (per-trade, все детали сделки)."""
    rows = []
    for sym, d in per_symbol.items():
        for t in d["ALL"]:
            rows.append({
                "symbol": sym.replace("USDT", ""),
                "preset": t.preset if hasattr(t, "preset") else "",
                "direction": t.direction,
                "entry_price": round(t.entry_price, 8),
                "exit_price": round(t.exit_price, 8),
                "entry_time": str(t.entry_time),
                "exit_time": str(t.exit_time),
                "pnl": round(t.pnl, 4),
                "exit_reason": t.exit_reason,
                "adx_at_entry": round(t.regime_adx, 1),
                "atr_pct": round(t.regime_atr_pct, 3),
                "regime_trend": t.regime_trend,
                "commission": getattr(t, "commission", 0.0),
                "entry_qty": t.qty,
                "sl_price": round(getattr(t, "sl_price", 0.0), 8),
                "tp_price": round(getattr(t, "tp1_price", 0.0), 8),
                "atr_value": round(getattr(t, "atr_value", 0.0), 8),
                "intrabar_return": round(getattr(t, "intrabar_return", 0.0), 3),
                "rsi_at_entry": round(getattr(t, "rsi_at_entry", 0.0), 1),
                "bases": "+".join(sorted(getattr(t, "voting_bases", []) or [])),
            })
    return rows


def summarize(trades, label):
    n = len(trades)
    if n == 0:
        print(f"  {label}: n=0")
        return None
    wins = sum(1 for t in trades if t.pnl > 0)
    pnl = sum(t.pnl for t in trades)
    avg_win = sum(t.pnl for t in trades if t.pnl > 0) / max(wins, 1)
    losses = [t for t in trades if t.pnl <= 0]
    avg_loss = sum(t.pnl for t in losses) / max(len(losses), 1)
    print(f"  {label}: n={n:<5} wr={wins/n*100:>5.1f}%  pnl={pnl:>9.2f}  "
          f"avg_win={avg_win:>6.3f} avg_loss={avg_loss:>7.3f}")
    return {"n": n, "wr": wins / n * 100, "pnl": pnl, "avg_win": avg_win, "avg_loss": avg_loss}


def main():
    parser = argparse.ArgumentParser(description="Backtest LONG vs SHORT over months (1h)")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--tp-mult", type=float, default=None,
                        help="Переопределить RR ATR-стопов: TP = tp_mult * SL (по умолчанию берётся из конфига, обычно 2.0)")
    parser.add_argument("--tp-mult-long", type=float, default=None,
                        help="RR только для LONG (асимметрично)")
    parser.add_argument("--tp-mult-short", type=float, default=None,
                        help="RR только для SHORT (асимметрично)")
    parser.add_argument("--consensus", type=int, default=1,
                        help="Минимум разных стратегий, согласных на направление (1=выкл, 2, 3)")
    parser.add_argument("--consensus-flat", type=int, default=None,
                        help="Consensus при ADX<15 (флет)")
    parser.add_argument("--consensus-weak", type=int, default=None,
                        help="Consensus при ADX 15-25 (слабый тренд)")
    parser.add_argument("--consensus-trend", type=int, default=None,
                        help="Consensus при ADX>=25 (тренд)")
    parser.add_argument("--block-short-trend", action="store_true",
                        help="Не открывать SHORT при ADX>=25")
    parser.add_argument("--block-hours", default=None,
                        help="Часы UTC для блокировки входа, через запятую (напр. 2,6,10,12,15,16)")
    parser.add_argument("--exclude-presets", default=None,
                        help="Пресеты для исключения, через запятую")
    parser.add_argument("--min-adx", type=float, default=None,
                        help="Входить только при ADX свечи >= порога (режим-фильтр)")
    parser.add_argument("--max-adx", type=float, default=None,
                        help="Входить только при ADX свечи <= порога (например 25 = только weak+flat)")
    parser.add_argument("--tp1-close-pct", type=float, default=None,
                        help="% позиции, закрываемой на TP1 (100 = целиком; 60 = частично + раннер)")
    parser.add_argument("--tp2-mult", type=float, default=None,
                        help="TP2 (раннер) = tp2_mult * SL. Если задан — TP2 дальше TP1")
    parser.add_argument("--timeframe", default="1h",
                        help="Таймфрейм бэктеста (1h, 15m, 5m...)")
    args = parser.parse_args()
    try:
        datetime.datetime.strptime(args.start, "%Y-%m-%d")
        datetime.datetime.strptime(args.end, "%Y-%m-%d")
    except ValueError:
        sys.exit("Start/end must be YYYY-MM-DD")

    symbols = get_symbol_list()
    print(f"Symbols: {len(symbols)}")
    if args.tp_mult_long or args.tp_mult_short:
        rr = f"LONG={args.tp_mult_long or 'cfg'}xSL SHORT={args.tp_mult_short or 'cfg'}xSL"
    elif args.tp_mult:
        rr = f"TP={args.tp_mult}xSL"
    else:
        rr = "RR из конфигов"
    adx_filter = ""
    if args.min_adx or args.max_adx:
        adx_filter = f", ADX filter: min={args.min_adx} max={args.max_adx}"
    if args.consensus_flat or args.consensus_weak or args.consensus_trend or args.block_short_trend:
        regime = (f"flat={args.consensus_flat or args.consensus} "
                  f"weak={args.consensus_weak or args.consensus} "
                  f"trend={args.consensus_trend or args.consensus}"
                  + (" SHORT-block" if args.block_short_trend else ""))
    else:
        regime = f"всегда={args.consensus}"
    print(f"Period: {args.start} -> {args.end} ({args.timeframe}), ATR SL/TP ({rr}), "
          f"consensus: {regime}, {len(PRESET_CONFIG)} presets, HTF off{adx_filter}"
          + (f", block_hours={args.block_hours}" if args.block_hours else "")
          + (f", exclude={len(args.exclude_presets.split(','))} presets" if args.exclude_presets else ""))
    print("=" * 80)

    per_symbol = asyncio.run(main_async(symbols, args.start, args.end,
                                        tp_mult=args.tp_mult,
                                        tp_mult_long=args.tp_mult_long,
                                        tp_mult_short=args.tp_mult_short,
                                        consensus=args.consensus,
                                        min_adx=args.min_adx,
                                        max_adx=args.max_adx,
                                        tp1_close_pct=args.tp1_close_pct,
                                        tp2_mult=args.tp2_mult,
                                        timeframe=args.timeframe,
                                        consensus_flat=args.consensus_flat,
                                        consensus_weak=args.consensus_weak,
                                        consensus_trend=args.consensus_trend,
                                        block_short_trend=args.block_short_trend,
                                        block_hours=args.block_hours,
                                        exclude_presets=args.exclude_presets))

    # Aggregate all trades per direction
    agg = {"LONG": [], "SHORT": [], "ALL": []}
    per_sym_rows = []
    for sym, d in per_symbol.items():
        for direction in ("LONG", "SHORT"):
            tr = d[direction]
            if not tr:
                continue
            wins = sum(1 for t in tr if t.pnl > 0)
            pnl = sum(t.pnl for t in tr)
            agg[direction].extend(tr)
            per_sym_rows.append({
                "symbol": sym, "direction": direction,
                "trades": len(tr), "winrate": round(wins / len(tr) * 100, 1),
                "pnl": round(pnl, 2),
            })
        agg["ALL"].extend(d["ALL"])

    print("=" * 80)
    print("ИТОГ ПО ВСЕМ СИМВОЛАМ:")
    for direction in ("ALL", "LONG", "SHORT"):
        summarize(agg[direction], direction)

    if agg["LONG"] and agg["SHORT"]:
        lp = sum(t.pnl for t in agg["LONG"])
        sp = sum(t.pnl for t in agg["SHORT"])
        ln = len(agg["LONG"])
        sn = len(agg["SHORT"])
        print("-" * 80)
        print(f"LONG avg/trade={lp/ln:+.4f} | SHORT avg/trade={sp/sn:+.4f} | "
              f"разница={(lp/ln)-(sp/sn):+.4f} на сделку")

    # Анализ пар: какие комбинации стратегий дают консенсус (consensus>1)
    from collections import Counter
    pair_stats = Counter()
    pair_pnl = defaultdict(float)
    pair_wins = Counter()
    for t in agg["ALL"]:
        bases = sorted(set(getattr(t, "voting_bases", []) or []))
        if len(bases) >= 2:
            key = "+".join(bases)
            pair_stats[key] += 1
            pair_pnl[key] += t.pnl
            if t.pnl > 0:
                pair_wins[key] += 1

    if pair_stats:
        print("\n" + "=" * 80)
        print("ТОП КОМБИНАЦИИ СТРАТЕГИЙ (консенсус, по количеству сделок):")
        rows_pairs = []
        for key, cnt in pair_stats.items():
            if cnt >= 10:
                rows_pairs.append((key, cnt, pair_wins[key] / cnt * 100, pair_pnl[key]))
        for key, cnt, wr, pnl in sorted(rows_pairs, key=lambda x: -x[3])[:25]:
            print(f"  {key:<55} n={cnt:<4} wr={wr:>5.1f}%  pnl={pnl:>8.2f}")
        print("\nХудшие комбинации:")
        for key, cnt, wr, pnl in sorted(rows_pairs, key=lambda x: x[3])[:10]:
            print(f"  {key:<55} n={cnt:<4} wr={wr:>5.1f}%  pnl={pnl:>8.2f}")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs",
                           f"backtest_direction_{timestamp}.csv")
    if per_sym_rows:
        fieldnames = list(per_sym_rows[0].keys())
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(per_sym_rows)
        print(f"\nSaved per-symbol detail -> {out_csv}")

    # Per-trade CSV: одна строка = одна сделка (для внешнего анализа)
    trade_rows = _trade_rows(per_symbol)
    if trade_rows:
        out_trades = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs",
                                  f"backtest_trades_{timestamp}.csv")
        fieldnames = list(trade_rows[0].keys())
        with open(out_trades, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(trade_rows)
        print(f"Saved per-trade detail ({len(trade_rows)} rows) -> {out_trades}")


if __name__ == "__main__":
    main()
