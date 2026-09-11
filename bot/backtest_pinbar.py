#!/usr/bin/env python3
"""
Pin Bar backtest — grid over definition params, both periods at once.

Уроки Inside Bar учтены:
- ослабленное определение (сетка параметров для набора выборки),
- данные скачиваются ОДИН раз на символ/период, все комбинации на тех же данных,
- оба периода (март-май + июнь-сен) с первого прогона,
- per-trade CSV со всеми метаданными (RSI, MACD, объём, контекст).

Pin Bar (бычий): длинная нижняя тень, тело у верха, small upper shadow, low — прокол за N.
Вход: следующая свеча закрылась выше high pin-бара (подтверждение пробоем).
SL = low pin-бара (за хвостом), TP = 2*SL.

Usage:
    python backtest_pinbar.py
    python backtest_pinbar.py --symbols BTCUSDT,ETHUSDT
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

# Сетка параметров Pin Bar
PIN_RATIOS = [1.5, 2.0, 2.5]       # нижняя/верхняя тень >= ratio * body
UPPER_RATIOS = [0.3, 0.5, 1.0]     # малая тень (противоположная) <= ratio * body
LOOKBACKS = [3, 5]                 # прокол: low[i] = min за N свечей (вкл. себя)
BODY_MIN = 0.0001                   # тело должно быть > 0 (не дожи)


def get_symbol_list():
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    symbols = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            symbols.append(sym)
    return sorted(set(symbols))


def ema(s, span): return s.ewm(span=span, adjust=False).mean()


def rsi(s, p=14):
    d = s.diff()
    g = d.clip(lower=0).rolling(p).mean()
    l = -d.clip(upper=0).rolling(p).mean()
    rs = g / l.replace(0, float("nan"))
    return (100 - 100 / (1 + rs)).fillna(50)


def macd_hist(s, f=12, sl=26, sg=9):
    ef = s.ewm(span=f, adjust=False).mean()
    es = s.ewm(span=sl, adjust=False).mean()
    m = ef - es
    return m - m.ewm(span=sg, adjust=False).mean()


def prepare(df):
    df = df.copy()
    df["volume_ma20"] = df["volume"].rolling(20).mean().shift(1)
    df["rsi14"] = rsi(df["close"], 14)
    df["ema50"] = ema(df["close"], 50)
    df["macd_hist"] = macd_hist(df["close"])
    pc = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return df


def detect_pin(df, i, ratio, upper_ratio, lookback):
    """Возвращает 'BULL'/'BEAR'/None для pin-бара на свече i."""
    if i < lookback:
        return None
    o, h, l, c = (float(df[k].iloc[i]) for k in ("open", "high", "low", "close"))
    body = abs(c - o)
    if body < BODY_MIN:
        return None
    rng = h - l
    if rng <= 0:
        return None
    lo_win = float(df["low"].iloc[i - lookback + 1:i + 1].min())
    hi_win = float(df["high"].iloc[i - lookback + 1:i + 1].max())
    lower_shadow = min(o, c) - l
    upper_shadow = h - max(o, c)

    # Бычий pin: длинная нижняя тень, тело у верха, прокол минимума
    if (lower_shadow >= ratio * body and upper_shadow <= upper_ratio * body
            and l <= lo_win):
        return "BULL"
    # Медвежий pin: длинная верхняя тень, тело у низа, прокол максимума
    if (upper_shadow >= ratio * body and lower_shadow <= upper_ratio * body
            and h >= hi_win):
        return "BEAR"
    return None


def simulate(df, entry_idx, direction, entry, sl, tp):
    """Следим со свечи entry_idx+1 (свеча входа закрыта)."""
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
        return pnl - comm, reason, ep, df.index[i], qty
    return None


async def run_grid(symbols):
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
                    df = prepare(df)
                    df.dropna(inplace=True)
                    for ratio in PIN_RATIOS:
                        for uratio in UPPER_RATIOS:
                            for lb in LOOKBACKS:
                                tag = f"r{ratio}_u{uratio}_lb{lb}"
                                trades = await process_symbol(df, sym, start, end, pname, tag, ratio, uratio, lb)
                                all_rows.extend(trades)
                except Exception as e:
                    print(f"{sym} {pname}: ERR {e}")
            print(f"[{si}/{len(symbols)}] {sym}: done")
    finally:
        await client.close_connection()
    return all_rows


async def process_symbol(df, sym, start, end, pname, tag, ratio, uratio, lb):
    rows = []
    n = len(df)
    i = lb
    while i < n - 1:
        pin = detect_pin(df, i, ratio, uratio, lb)
        if pin is None:
            i += 1
            continue
        # подтверждение: следующая свеча закрылась за экстремумом pin
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
        if direction == "LONG":
            sl = lo_pin
            tp = entry + 2 * (entry - sl)
        else:
            sl = hi_pin
            tp = entry - 2 * (sl - entry)

        # объём подтверждения на следующей свече
        vma = float(df["volume_ma20"].iloc[nxt])
        vratio = float(df["volume"].iloc[nxt]) / vma if vma and vma > 0 else 0

        res = simulate(df, nxt, direction, entry, sl, tp)
        if res is None:
            # не закрылась до конца данных — пропускаем (вход был, но статистики нет)
            i = nxt + 1
            continue
        pnl, reason, ep, exit_t, qty = res

        ema_now = float(df["ema50"].iloc[i])
        ema_prev = float(df["ema50"].iloc[i - 10]) if i >= 10 else ema_now
        slope = (ema_now - ema_prev) / ema_prev * 100 if ema_prev else 0
        trend = "uptrend" if slope > 0.5 else ("downtrend" if slope < -0.5 else "flat")
        ctype = ("continuation" if (direction == "LONG" and trend == "uptrend")
                 or (direction == "SHORT" and trend == "downtrend")
                 else ("reversal" if trend != "flat" else "flat"))

        rows.append({
            "symbol": sym.replace("USDT", ""), "period": f"{start}_{end}",
            "param_tag": tag, "consolidation_type": ctype,
            "direction": direction, "entry_price": round(entry, 8),
            "exit_price": round(ep, 8), "entry_time": str(df.index[nxt]),
            "exit_time": str(exit_t), "pnl": round(pnl, 4),
            "exit_reason": reason, "sl_price": round(sl, 8), "tp_price": round(tp, 8),
            "rsi_at_entry": round(float(df["rsi14"].iloc[nxt]), 1),
            "ema50_slope_pct": round(slope, 3),
            "macd_at_start": round(float(df["macd_hist"].iloc[i]), 8),
            "breakout_volume_ratio": round(vratio, 2),
            "adx_at_entry": 0.0,
            "pin_size_pct": round((hi_pin - lo_pin) / lo_pin * 100, 4),
        })
        i = nxt + 1  # одна сделка после pin; дальше ищем новый pin
    return rows


def main():
    parser = argparse.ArgumentParser(description="Pin Bar grid backtest")
    parser.add_argument("--symbols", default=None)
    args = parser.parse_args()

    symbols = get_symbol_list()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]

    total_combos = len(PIN_RATIOS) * len(UPPER_RATIOS) * len(LOOKBACKS)
    print(f"Symbols: {len(symbols)} | Periods: 2 | Combos: {total_combos}")
    print(f"Grid: ratios={PIN_RATIOS} upper={UPPER_RATIOS} lookback={LOOKBACKS}")

    rows = asyncio.run(run_grid(symbols))

    if not rows:
        print("Нет сделок")
        return

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", f"pinbar_{ts}.csv")
    fn = list(rows[0].keys())
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fn)
        w.writeheader()
        w.writerows(rows)
    print(f"\nSaved -> {out} (total {len(rows)} rows)")

    # Сводка по комбинациям параметров
    print("\n=== Сводка по комбо (оба периода) ===")
    by_tag = defaultdict(list)
    for r in rows:
        by_tag[r["param_tag"]].append(r)
    for tag in sorted(by_tag):
        items = by_tag[tag]
        n = len(items)
        wins = sum(1 for x in items if float(x["pnl"]) > 0)
        pnl = sum(float(x["pnl"]) for x in items)
        print(f"  {tag:<14} n={n:<6} wr={wins/n*100:>5.1f}%  pnl={pnl:>9.2f}")


if __name__ == "__main__":
    main()
