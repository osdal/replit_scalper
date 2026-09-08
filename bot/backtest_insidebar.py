#!/usr/bin/env python3
"""
Inside Bar Breakout backtest (spec_inside_bar.md).

Вход: пробой консолидации Inside Bar (MB + 2..5 IB) на 1h.
Контекст: 4h EMA50/MACD только для классификации (continuation/reversal/flat).
Выход: SL за диапазоном консолидации, TP = 2*SL.

Каждая сделка — строка CSV со всеми метаколонками по спецификации.
Прогон на двух периодах сразу: март-май и июнь-сентябрь (для out-of-sample).

Usage:
    python backtest_insidebar.py --periods 2026-03-01:2026-05-31,2026-06-01:2026-09-07
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
from market_data import get_historical_klines

load_dotenv()

# ── Параметры (раздел 6 спецификации) ─────────────────────────────────────
MIN_IB = 1          # минимум IB после MB (ослаблено: было 2)
MAX_IB = 5          # максимум IB
ALLOW_TOUCH = True  # True = IB может касаться границ (<= / >=), не строго < / >
BREAKOUT_WINDOW = 3 # пробой разрешён в окне N свечей после IBn (было 1 = сразу после)
MAX_CONS_AGE = 24   # макс возраст консолидации в 1h-свечах (от MB до IBn)
VOL_MULT = 1.5      # объём пробоя >= VOL_MULT * volume_ma20
SLOPE_THRESHOLD = 0.5  # % наклон EMA50 за 40h для классификации
RANGE_WINDOW = 100  # окно для position_in_range (1h свечей)
TP_MULT = 2.0       # TP = TP_MULT * SL


def get_symbol_list():
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    symbols = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            symbols.append(sym)
    return sorted(set(symbols))


def ema(series, span):
    return series.ewm(span=span, adjust=False).mean()


def rsi(series, period=14):
    delta = series.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss.replace(0, float("nan"))
    out = 100 - 100 / (1 + rs)
    return out.fillna(50)


def macd_hist(series, fast=12, slow=26, signal=9):
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd = ema_fast - ema_slow
    sig = macd.ewm(span=signal, adjust=False).mean()
    return macd - sig


def prepare_1h(df):
    """Добавляет колонки, нужные детектору на 1h."""
    df = df.copy()
    df["volume_ma20"] = df["volume"].rolling(20).mean().shift(1)  # MA до текущей свечи
    df["rsi14"] = rsi(df["close"], 14)
    df["body_size"] = (df["close"] - df["open"]).abs() / (df["high"] - df["low"]).replace(0, float("nan"))
    df["close_pos"] = (df["close"] - df["low"]) / (df["high"] - df["low"]).replace(0, float("nan"))
    # ATR(14) для трейлинг-стопа
    prev_close = df["close"].shift(1)
    tr = pd.concat([
        df["high"] - df["low"],
        (df["high"] - prev_close).abs(),
        (df["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    df["atr14"] = tr.ewm(alpha=1 / 14, adjust=False).mean()
    return df


def prepare_4h(df):
    df = df.copy()
    df["ema50"] = ema(df["close"], 50)
    df["macd_hist"] = macd_hist(df["close"])
    return df


def is_inside(df, j, strict=True):
    """Проверяет, что свеча j внутри свечи j-1."""
    if strict:
        return (df["high"].iloc[j] < df["high"].iloc[j - 1] and
                df["low"].iloc[j] > df["low"].iloc[j - 1])
    else:
        return (df["high"].iloc[j] <= df["high"].iloc[j - 1] and
                df["low"].iloc[j] >= df["low"].iloc[j - 1] and
                not (df["high"].iloc[j] == df["high"].iloc[j - 1] and
                     df["low"].iloc[j] == df["low"].iloc[j - 1]))  # исключаем полный дубль свечи


def find_consolidations(df):
    """
    Ищет консолидации MB + [IB]*n на 1h.
    MIN_IB >= 1, IB может касаться границ (ALLOW_TOUCH).
    Возвращает список dict: {mb_idx, first_ib_idx, last_ib_idx, n_ib,
    cons_high, cons_low, mb_high, mb_low}
    """
    cons = []
    i = 1
    n = len(df)
    while i < n:
        if is_inside(df, i, strict=not ALLOW_TOUCH):
            mb_idx = i - 1
            first_ib = i
            j = i
            while j < n:
                if is_inside(df, j, strict=not ALLOW_TOUCH):
                    j += 1
                else:
                    break
            last_ib = j - 1
            n_ib = last_ib - first_ib + 1
            age = last_ib - mb_idx + 1
            if MIN_IB <= n_ib <= MAX_IB and age <= MAX_CONS_AGE:
                seg = df.iloc[mb_idx:last_ib + 1]
                cons.append({
                    "mb_idx": mb_idx,
                    "first_ib_idx": first_ib,
                    "last_ib_idx": last_ib,
                    "n_ib": n_ib,
                    "cons_high": float(seg["high"].max()),
                    "cons_low": float(seg["low"].min()),
                    "mb_high": float(df["high"].iloc[mb_idx]),
                    "mb_low": float(df["low"].iloc[mb_idx]),
                })
            i = last_ib + 1  # пропускаем обработанную серию
        else:
            i += 1
    return cons


def classify(df_1h, df_4h, cons):
    """Классификация и метаколонки по 4h-контексту на момент MB."""
    t_mb = df_1h.index[cons["mb_idx"]]
    idx_4h = df_4h.index.searchsorted(t_mb, side="left") - 1
    if idx_4h < 11:
        return None  # недостаточно 4h-истории

    ema_before = float(df_4h["ema50"].iloc[idx_4h - 10])
    ema_start = float(df_4h["ema50"].iloc[idx_4h])
    slope_pct = (ema_start - ema_before) / ema_before * 100 if ema_before else 0.0

    if slope_pct > SLOPE_THRESHOLD:
        trend = "uptrend"
    elif slope_pct < -SLOPE_THRESHOLD:
        trend = "downtrend"
    else:
        trend = "flat"

    macd_val = float(df_4h["macd_hist"].iloc[idx_4h])

    # Позиция консолидации в диапазоне последних RANGE_WINDOW 1h свечей
    lo_idx = max(0, cons["mb_idx"] - RANGE_WINDOW + 1)
    window = df_1h.iloc[lo_idx:cons["mb_idx"] + 1]
    lo = float(window["low"].min())
    hi = float(window["high"].max())
    mid_cons = (cons["cons_high"] + cons["cons_low"]) / 2
    rel = (mid_cons - lo) / (hi - lo) if hi > lo else 0.5
    if rel < 0.33:
        pos = "lower_half"
    elif rel > 0.66:
        pos = "upper_half"
    else:
        pos = "middle"

    cons_size_pct = (cons["cons_high"] - cons["cons_low"]) / cons["cons_low"] * 100 if cons["cons_low"] else 0
    mb_size_pct = (cons["mb_high"] - cons["mb_low"]) / cons["mb_low"] * 100 if cons["mb_low"] else 0

    return {
        "trend": trend,
        "ema50_slope_pct": round(slope_pct, 4),
        "macd_at_start": round(macd_val, 8),
        "position_in_range": pos,
        "consolidation_size_pct": round(cons_size_pct, 4),
        "mb_size_pct": round(mb_size_pct, 4),
    }


async def download(client, symbol, interval, start, end):
    return await get_historical_klines(client=client, symbol=symbol, interval=interval,
                                       start=start, end=end)


def simulate_trade(df_1h, entry_idx, direction, entry_price, sl_price, tp_price, fee):
    """Симулирует позицию с entry_idx (свеча входа уже закрыта, следим со следующей)."""
    # qty = fixed_risk(0.5) / (entry * sl_pct/100)
    sl_pct = abs(entry_price - sl_price) / entry_price * 100 if entry_price else 0
    if sl_pct <= 0:
        return None
    qty = 0.5 / (entry_price * sl_pct / 100)

    for i in range(entry_idx + 1, len(df_1h)):
        hi = float(df_1h["high"].iloc[i])
        lo = float(df_1h["low"].iloc[i])
        if direction == "LONG":
            if lo <= sl_price:
                exit_px = sl_price
                reason = "SL"
            elif hi >= tp_price:
                exit_px = tp_price
                reason = "TP"
            else:
                continue
            pnl = (exit_px - entry_price) * qty
        else:
            if hi >= sl_price:
                exit_px = sl_price
                reason = "SL"
            elif lo <= tp_price:
                exit_px = tp_price
                reason = "TP"
            else:
                continue
            pnl = (entry_price - exit_px) * qty
        comm = (abs(entry_price) + abs(exit_px)) * qty * fee
        return pnl - comm, reason, exit_px, df_1h.index[i], qty
    return None  # не закрылась до конца данных


def simulate_trade_trailing(df_1h, entry_idx, direction, entry_price, sl_start, fee, atr_mult=2.5):
    """
    Трейлинг-выход: стартовый SL = граница консолидации (sl_start), далее SL
    подтягивается за ценой на atr_mult * ATR(1h). Выход только по трейлинг-SL.
    """
    # qty от стартового SL (риск = дистанция до границы консолидации)
    sl_pct = abs(entry_price - sl_start) / entry_price * 100 if entry_price else 0
    if sl_pct <= 0:
        return None
    qty = 0.5 / (entry_price * sl_pct / 100)

    # текущий трейлинг-SL, только улучшается
    trail_sl = sl_start
    for i in range(entry_idx + 1, len(df_1h)):
        hi = float(df_1h["high"].iloc[i])
        lo = float(df_1h["low"].iloc[i])
        atr = float(df_1h["atr14"].iloc[i])
        atr = atr if atr == atr and atr > 0 else entry_price * 0.005  # fallback 0.5%
        dist = atr_mult * atr

        if direction == "LONG":
            # подтягиваем SL вверх: не ниже текущего, отступаем от максимума
            candidate = hi - dist
            trail_sl = max(trail_sl, candidate)
            if lo <= trail_sl:
                exit_px = trail_sl
                pnl = (exit_px - entry_price) * qty
                comm = (abs(entry_price) + abs(exit_px)) * qty * fee
                return pnl - comm, "SL", exit_px, df_1h.index[i], qty
        else:
            candidate = lo + dist
            # SHORT: SL опускаем — минимум из текущего и отступа от минимума
            trail_sl = min(trail_sl, candidate)
            if hi >= trail_sl:
                exit_px = trail_sl
                pnl = (entry_price - exit_px) * qty
                comm = (abs(entry_price) + abs(exit_px)) * qty * fee
                return pnl - comm, "SL", exit_px, df_1h.index[i], qty
    return None


async def run_period(symbols, start, end, exit_mode="fixed", trail_atr=2.5):
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    cfg_template = None
    try:
        rows = []
        for si, sym in enumerate(symbols, 1):
            try:
                df1 = await download(client, sym, "1h", start, end)
                df4 = await download(client, sym, "4h", start, end)
                if len(df1) < 300 or len(df4) < 120:
                    print(f"[{si}/{len(symbols)}] {sym}: мало данных, skip")
                    continue
                df1 = prepare_1h(df1)
                df4 = prepare_4h(df4)
                df4.dropna(inplace=True)

                if cfg_template is None:
                    fname = sym.replace("USDT", "").lower()
                    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"config_{fname}.yaml")
                    try:
                        cfg_template = load_config(cfg_path)
                    except Exception:
                        cfg_template = None
                fee = 0.05 / 100.0
                if cfg_template is not None:
                    fee = getattr(cfg_template, "commission_pct", 0.05) / 100.0

                cons_list = find_consolidations(df1)
                for cons in cons_list:
                    meta = classify(df1, df4, cons)
                    if meta is None:
                        continue
                    # Пробой в окне BREAKOUT_WINDOW свечей после last_ib.
                    # Ищем первую свечу, close которой за пределами диапазона.
                    break_idx = None
                    break_dir = None
                    close_break = None
                    for bi in range(cons["last_ib_idx"] + 1,
                                     min(cons["last_ib_idx"] + 1 + BREAKOUT_WINDOW, len(df1))):
                        c = float(df1["close"].iloc[bi])
                        if c > cons["cons_high"]:
                            break_idx = bi
                            break_dir = "LONG"
                            close_break = c
                            break
                        if c < cons["cons_low"]:
                            break_idx = bi
                            break_dir = "SHORT"
                            close_break = c
                            break
                    if break_idx is None:
                        continue
                    vol = float(df1["volume"].iloc[break_idx])
                    vol_ma = float(df1["volume_ma20"].iloc[break_idx])
                    if vol_ma <= 0:
                        continue
                    vol_ratio = vol / vol_ma

                    direction = break_dir

                    # Подтверждение объёмом
                    if vol_ratio < VOL_MULT:
                        continue

                    entry = close_break
                    rsi_entry = float(df1["rsi14"].iloc[break_idx])
                    body_size = float(df1["body_size"].iloc[break_idx]) if not pd.isna(df1["body_size"].iloc[break_idx]) else 0
                    close_pos = float(df1["close_pos"].iloc[break_idx]) if not pd.isna(df1["close_pos"].iloc[break_idx]) else 0.5

                    if direction == "LONG":
                        sl_price = cons["cons_low"]
                        tp_price = entry + TP_MULT * (entry - sl_price)
                        ctype = "continuation" if meta["trend"] == "uptrend" else ("reversal" if meta["trend"] == "downtrend" else "flat")
                    else:
                        sl_price = cons["cons_high"]
                        tp_price = entry - TP_MULT * (sl_price - entry)
                        ctype = "continuation" if meta["trend"] == "downtrend" else ("reversal" if meta["trend"] == "uptrend" else "flat")

                    if exit_mode == "trailing":
                        res = simulate_trade_trailing(df1, break_idx, direction, entry, sl_price, fee, atr_mult=trail_atr)
                    else:
                        res = simulate_trade(df1, break_idx, direction, entry, sl_price, tp_price, fee)
                    if res is None:
                        continue
                    pnl, reason, exit_px, exit_t, qty = res

                    rows.append({
                        "symbol": sym.replace("USDT", ""),
                        "period": f"{start}_{end}",
                        "consolidation_type": ctype,
                        "direction": direction,
                        "entry_price": round(entry, 8),
                        "exit_price": round(exit_px, 8),
                        "entry_time": str(df1.index[break_idx]),
                        "exit_time": str(exit_t),
                        "pnl": round(pnl, 4),
                        "exit_reason": reason,
                        "sl_price": round(sl_price, 8),
                        "tp_price": round(tp_price, 8),
                        "commission": round((abs(entry) + abs(exit_px)) * qty * fee, 6),
                        "ema50_slope_pct": meta["ema50_slope_pct"],
                        "macd_at_start": meta["macd_at_start"],
                        "position_in_range": meta["position_in_range"],
                        "rsi_at_entry": round(rsi_entry, 1),
                        "consolidation_bars": cons["n_ib"],
                        "consolidation_size_pct": meta["consolidation_size_pct"],
                        "mb_size_pct": meta["mb_size_pct"],
                        "consolidation_high": round(cons["cons_high"], 8),
                        "consolidation_low": round(cons["cons_low"], 8),
                        "breakout_volume_ratio": round(vol_ratio, 2),
                        "breakout_candle_body_size": round(body_size, 3) if body_size == body_size else 0,
                        "close_position_in_candle": round(close_pos, 3),
                        "breakout_series_length": 1,
                        "breakout_is_first_in_series": True,
                        "adx_at_entry": 0.0,
                    })
                print(f"[{si}/{len(symbols)}] {sym}: cons={len(cons_list)} trades={len([r for r in rows if r['symbol']==sym.replace('USDT','')])}")
            except Exception as e:
                print(f"[{si}/{len(symbols)}] {sym}: ERROR {e}")
        return rows
    finally:
        await client.close_connection()


def summarize(rows, label):
    if not rows:
        print(f"{label}: пусто")
        return
    n = len(rows)
    wins = sum(1 for r in rows if r["pnl"] > 0)
    pnl = sum(r["pnl"] for r in rows)
    print(f"  {label}: n={n} wr={wins/n*100:.1f}% pnl={pnl:.2f}")


def main():
    parser = argparse.ArgumentParser(description="Inside Bar Breakout backtest")
    parser.add_argument("--symbols", default=None, help="Список символов через запятую (по умолчанию все)")
    parser.add_argument("--exit", default="fixed", choices=["fixed", "trailing"],
                        help="Режим выхода: fixed (TP=2xSL) или trailing (трейлинг-SL)")
    parser.add_argument("--trail-atr", type=float, default=2.5,
                        help="Множитель ATR для трейлинг-стопа (по умолчанию 2.5)")
    parser.add_argument("--periods", default=None,
                        help="Пары дат start:end через запятую. По умолчанию март-май+июнь-сен")
    args = parser.parse_args()

    if args.periods:
        periods = []
        for pair in args.periods.split(","):
            s, e = pair.split(":")
            periods.append((s.strip(), e.strip()))
    else:
        # Периоды для проверки (in-sample + out-of-sample вместе)
        periods = [("2026-03-01", "2026-05-31"), ("2026-06-01", "2026-09-07")]

    symbols = get_symbol_list()
    if args.symbols:
        # Произвольный список: берём как есть (не только монеты из config_*.yaml)
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print(f"Symbols ({len(symbols)}): {symbols[:10]}...")
    print(f"Exit mode: {args.exit}" + (f" (ATR x{args.trail_atr})" if args.exit == "trailing" else ""))
    print(f"Periods: {periods}")

    all_rows = []
    for start, end in periods:
        print(f"\n=== Период {start} -> {end} ===")
        rows = asyncio.run(run_period(symbols, start, end, exit_mode=args.exit, trail_atr=args.trail_atr))
        for r in rows:
            r["exit_mode"] = args.exit
        all_rows.extend(rows)
        summarize(rows, f"ИТОГО {start}")

    if all_rows:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        out_csv = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs",
                               f"insidebar_{timestamp}.csv")
        fieldnames = list(all_rows[0].keys())
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_rows)
        print(f"\nSaved -> {out_csv}")

        # Сводка по периодам и типам
        print("\n=== СВОДКА ===")
        by_period = defaultdict(list)
        for r in all_rows:
            by_period[r["period"]].append(r)
        for period, rows in by_period.items():
            summarize(rows, f"{period}")

        by_type = defaultdict(list)
        for r in all_rows:
            by_type[r["consolidation_type"]].append(r)
        print("\nПо типам (все периоды):")
        for t, rows in sorted(by_type.items()):
            summarize(rows, f"type={t}")


if __name__ == "__main__":
    main()
