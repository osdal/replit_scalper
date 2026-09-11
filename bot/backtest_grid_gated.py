#!/usr/bin/env python3
"""
Grid + ADX gate (методика xbot.live / Freya Academy).

Grid прибылен в боковике (ADX<20), убыточен в тренде (ADX>25).
Гейт: исполнение сетки (покупки/продажи) разрешено ТОЛЬКО когда ADX < gate.
Когда ADX >= gate — grid стоит (не открывает новые лоты). Открытые лоты остаются,
но не пополняются; при возврате ADX в боковик grid возобновляет работу.

Дополнительно: диапазон сетки = max(истор. high-low за lookback, gate_min_range)
(2*ATR как минимум — из Freya).

Проверка на 2 периодах, сетка порогов ADX и конфигов.
"""

import argparse
import asyncio
import csv
import datetime
import os
import sys
from collections import defaultdict, deque

import numpy as np
import pandas as pd
from binance import AsyncClient
from dotenv import load_dotenv

from market_data import get_historical_klines

load_dotenv()

PERIODS = [("2026-03-01", "2026-05-31", "mar_may"), ("2026-06-01", "2026-09-07", "jun_sep")]
FEE = 0.0005

GATES = [15, 20, 25, 99]  # 99 = без гейта (контроль)
GRID_CFGS = [
    {"n_levels": 20, "lookback": 192, "rebuild": 96},
    {"n_levels": 10, "lookback": 192, "rebuild": 96},
]


def get_symbol_list():
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    symbols = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            symbols.append(sym)
    return sorted(set(symbols))


def compute_adx(df, period=14):
    """Классический ADX."""
    hi = df["high"].astype(float)
    lo = df["low"].astype(float)
    cl = df["close"].astype(float)
    up = hi.diff()
    dn = -lo.diff()
    plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
    tr1 = hi - lo
    tr2 = (hi - cl.shift(1)).abs()
    tr3 = (lo - cl.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / period, adjust=False).mean()
    pdi = 100 * plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    mdi = 100 * minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan)
    dx = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean()


def compute_atr(df, period=14):
    pc = df["close"].astype(float).shift(1)
    tr = pd.concat([df["high"].astype(float) - df["low"].astype(float),
                    (df["high"].astype(float) - pc).abs(),
                    (df["low"].astype(float) - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def realized_vol_annual(df, period=48, candles_per_year=2190):
    """Реализованная годовая волатильность (%), скользящая за period свечей.
    Для 4h: ~2190 свечей/год (6 в день). period=48 -> ~8 дней."""
    logret = np.log(df["close"].astype(float) / df["close"].astype(float).shift(1))
    rv = logret.rolling(period).std() * np.sqrt(candles_per_year)
    return rv * 100  # в %


def run_grid_gated(df, cfg, gate, capital=1000.0, leverage=5.0, fee=FEE, sl_buffer=0.02,
                   range_mode="minmax", atr_mult=2.5, grid_type="arithmetic",
                   geom_threshold_pct=20.0, vol_min=None, vol_max=None,
                   candles_per_year=2190):
    """
    Grid по методике Freya/xbot:
    - сетка уровней в диапазоне [lo, hi],
      range_mode="minmax" — lo/hi = min(low)/max(high) за lookback;
      range_mode="atr"    — lo/hi = центр ± atr_mult*ATR (методика Freya: 2.5*ATR ~95% движений);
      grid_type="arithmetic" — равный $ между уровнями;
      grid_type="geometric"  — равный % между уровнями (для диапазонов >20%, Freya);
      grid_type="auto"       — arithmetic при ширине диапазона < geom_threshold_pct%,
                               geometric при ширине >= geom_threshold_pct% (рекомендация Freya);
    - в боковике (ADX < gate): покупаем на уровнях вниз, продаём на уровнях вверх,
    - в тренде (ADX >= gate): ПОКУПКИ ОСТАНОВЛЕНЫ (не накапливаем), продажи продолжаются
      (разгружаем инвентарь при откатах вверх),
    - STOP-LOSS: если цена ушла ниже lo * (1 - sl_buffer) — принудительно закрываем ВЕСЬ
      инвентарь (фиксируем убыток, не держим «мешки»),
    - FIFO-закрытие, комиссия, нереализованный PnL по последней цене.
    """
    n = len(df)
    n_levels = cfg["n_levels"]
    lookback = cfg["lookback"]
    rebuild = cfg["rebuild"]

    adx = compute_adx(df).fillna(0)
    atr = compute_atr(df)
    if vol_min is not None or vol_max is not None:
        rvol = realized_vol_annual(df, period=cfg.get("vol_lookback", 48), candles_per_year=candles_per_year).fillna(0)
    else:
        rvol = None

    lots = deque()          # цены покупки открытых лотов
    realized = 0.0
    exec_count = 0
    sl_hit = 0              # сколько раз срабатывал стоп
    levels = []
    lo = hi = 0.0

    def build_levels(idx):
        nonlocal lo, hi, levels
        start = max(0, idx - lookback + 1)
        if range_mode == "atr":
            # ATR-диапазон вокруг последней цены (методика Freya)
            ref_c = float(df["close"].iloc[idx])
            atr_v = float(atr.iloc[idx])
            atr_v = atr_v if atr_v == atr_v and atr_v > 0 else ref_c * 0.02
            lo = ref_c - atr_mult * atr_v
            hi = ref_c + atr_mult * atr_v
        else:
            lo = float(df["low"].iloc[start:idx + 1].min())
            hi = float(df["high"].iloc[start:idx + 1].max())
        if hi <= lo:
            return
        # выбор типа сетки по ширине диапазона
        width_pct = (hi / lo - 1) * 100 if lo > 0 else 0
        use_geom = (grid_type == "geometric") or (grid_type == "auto" and width_pct >= geom_threshold_pct)
        if use_geom and lo > 0:
            # равный процент между уровнями
            ratio = (hi / lo) ** (1.0 / n_levels)
            levels = [lo * (ratio ** k) for k in range(1, n_levels)]
        else:
            step = (hi - lo) / n_levels
            levels = [lo + step * k for k in range(1, n_levels)]

    def lot_qty(ref_price):
        notional_per_level = capital * leverage / n_levels
        return notional_per_level / ref_price if ref_price else 0.0

    def close_all_at(px):
        nonlocal realized, exec_count
        while lots:
            buy_px = lots.popleft()
            pnl = (px - buy_px) * qty - (px + buy_px) * qty * fee
            realized += pnl
            exec_count += 1

    start_idx = min(n - 1, lookback)
    build_levels(start_idx)
    prev_price = float(df["close"].iloc[start_idx])
    ref = (lo + hi) / 2 if hi > lo else prev_price
    qty = lot_qty(ref)

    for i in range(start_idx + 1, n):
        h = float(df["high"].iloc[i])
        l = float(df["low"].iloc[i])
        c = float(df["close"].iloc[i])

        if (i - lookback) % rebuild == 0 or not levels:
            build_levels(i)
            ref = (lo + hi) / 2 if hi > lo else prev_price
            qty = lot_qty(ref)

        adx_i = float(adx.iloc[i])
        trending = adx_i >= gate
        # фильтр волатильности: grid (покупки) только при vol в диапазоне
        if rvol is not None:
            v = float(rvol.iloc[i])
            vol_ok = (vol_min is None or v >= vol_min) and (vol_max is None or v <= vol_max)
        else:
            vol_ok = True

        # 1) STOP-LOSS: пробитие нижней границы диапазона с буфером -> закрыть всё
        sl_price = lo * (1 - sl_buffer)
        if l <= sl_price:
            close_all_at(sl_price)
            sl_hit += 1
            prev_price = c
            continue

        # 2) Покупки ТОЛЬКО в боковике и при допустимой волатильности
        if not trending and vol_ok:
            for lv in sorted(levels, reverse=True):
                if lv < prev_price and l <= lv:
                    open_notional = sum(b * qty for b in lots) + lv * qty
                    if open_notional > capital * leverage:
                        continue
                    lots.append(lv)
                    realized -= lv * qty * fee
                    exec_count += 1

        # 3) Продажи ВСЕГДА (и в боковике, и в тренде на откатах вверх — разгрузка)
        for lv in sorted(levels):
            if lv > prev_price and h >= lv and lots:
                buy_px = lots.popleft()
                pnl = (lv - buy_px) * qty - (lv + buy_px) * qty * fee
                realized += pnl
                exec_count += 1

        prev_price = c

    # финальная оценка оставшегося инвентаря
    last = float(df["close"].iloc[n - 1])
    unreal = sum((last - b) * qty for b in lots) - sum(b * qty * fee for b in lots)
    total = realized + unreal
    return total, total / capital * 100, exec_count, len(lots), sl_hit


async def run(symbols, capital, leverage, periods=None, timeframe="1h",
              range_mode="minmax", atr_mult=2.5, grid_type="arithmetic",
              geom_threshold_pct=20.0, vol_min=None, vol_max=None, candles_per_year=2190):
    if periods is None:
        periods = PERIODS
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    all_rows = []
    try:
        for si, sym in enumerate(symbols, 1):
            for start, end, pname in periods:
                try:
                    df = await get_historical_klines(client=client, symbol=sym, interval=timeframe, start=start, end=end)
                    if len(df) < 400:
                        continue
                    for gcfg in GRID_CFGS:
                        tag_cfg = f"L{gcfg['n_levels']}_lb{gcfg['lookback']}"
                        for gate in GATES:
                            pnl, pct, n_exec, open_lots, sl_hit = run_grid_gated(
                                df, gcfg, gate, capital, leverage, range_mode=range_mode,
                                atr_mult=atr_mult, grid_type=grid_type,
                                geom_threshold_pct=geom_threshold_pct,
                                vol_min=vol_min, vol_max=vol_max,
                                candles_per_year=candles_per_year)
                            all_rows.append({
                                "symbol": sym.replace("USDT", ""), "period": f"{start}_{end}",
                                "cfg": tag_cfg, "gate": gate, "range_mode": range_mode,
                                "grid_type": grid_type, "vol_min": vol_min, "vol_max": vol_max,
                                "pnl": round(pnl, 2), "pct": round(pct, 2),
                                "exec": n_exec, "open_lots": open_lots, "sl_hit": sl_hit,
                            })
                except Exception as e:
                    print(f"{sym} {pname}: ERR {e}")
            print(f"[{si}/{len(symbols)}] {sym}: done")
    finally:
        await client.close_connection()
    return all_rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default=None)
    parser.add_argument("--capital", type=float, default=1000.0)
    parser.add_argument("--leverage", type=float, default=5.0)
    parser.add_argument("--periods", default=None,
                        help="Пары start:end:name через запятую (по умолчанию mar_may+jun_sep)")
    parser.add_argument("--timeframe", default="1h", help="Таймфрейм (1h, 4h)")
    parser.add_argument("--range-mode", default="minmax", choices=["minmax", "atr"],
                        help="Границы диапазона: minmax (истор.) или atr (центр ± mult*ATR)")
    parser.add_argument("--atr-mult", type=float, default=2.5, help="Множитель ATR для диапазона")
    parser.add_argument("--grid-type", default="arithmetic",
                        choices=["arithmetic", "geometric", "auto"],
                        help="Тип сетки: arithmetic (равный $), geometric (равный %), "
                             "auto (arithmetic для узких, geometric для широких)")
    parser.add_argument("--geom-threshold", type=float, default=20.0,
                        help="Порог ширины диапазона % для auto-режима")
    parser.add_argument("--vol-min", type=float, default=None,
                        help="Мин. годовая волатильность % для работы grid (по xbot: 30)")
    parser.add_argument("--vol-max", type=float, default=None,
                        help="Макс. годовая волатильность % (по xbot: 80-120, выше = шок)")
    args = parser.parse_args()
    symbols = get_symbol_list()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if args.periods:
        periods = []
        for pair in args.periods.split(","):
            s, e, nm = pair.split(":")
            periods.append((s.strip(), e.strip(), nm.strip()))
    else:
        periods = None
    # Конфиги в свечах выбранного ТФ: окно 8 дней, перестройка 4 дня
    # 1h: 192 свечи = 8 дней. 4h: 48 свечей = 8 дней (эквивалент).
    candles_per_day = {"1h": 24, "4h": 6}.get(args.timeframe, 6)
    global GRID_CFGS
    GRID_CFGS = [
        {"n_levels": 20, "lookback": 8 * candles_per_day, "rebuild": 4 * candles_per_day},
        {"n_levels": 10, "lookback": 8 * candles_per_day, "rebuild": 4 * candles_per_day},
    ]
    cpy = {"1h": 8760, "4h": 2190}.get(args.timeframe, 2190)
    print(f"Symbols: {len(symbols)} | gates={GATES} | tf={args.timeframe} | range={args.range_mode} "
          f"grid={args.grid_type} | vol=[{args.vol_min},{args.vol_max}] | cfgs={len(GRID_CFGS)} | periods={'custom' if periods else 2}")

    rows = asyncio.run(run(symbols, args.capital, args.leverage, periods, args.timeframe,
                           args.range_mode, args.atr_mult, args.grid_type, args.geom_threshold,
                           args.vol_min, args.vol_max, cpy))
    if not rows:
        print("Нет данных")
        return
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", f"grid_gated_{ts}.csv")
    fn = list(rows[0].keys())
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fn)
        w.writeheader()
        w.writerows(rows)
    print(f"\nSaved -> {out}")

    # Сводка по cfg x gate
    print("\n=== Grid gated + SL: сумма PnL по cfg x gate ===")
    agg = defaultdict(lambda: {"n": 0, "pnl": 0.0, "win": 0, "exec": 0, "sl": 0})
    for r in rows:
        k = (r["cfg"], r["gate"])
        a = agg[k]
        a["n"] += 1
        a["pnl"] += float(r["pnl"])
        a["exec"] += int(r["exec"])
        a["sl"] += int(r.get("sl_hit", 0))
        if float(r["pnl"]) > 0:
            a["win"] += 1
    for (cfg, gate_s) in sorted(agg, key=lambda kv: (kv[0], str(kv[1]))):
        gate = int(gate_s)
        a = agg[(cfg, gate_s)]
        label = f"gate={gate}" if gate < 99 else "no-gate"
        print(f"  {cfg:<14} {label:<9} sum_pnl={a['pnl']:>10.2f} win={a['win']}/{a['n']} exec={a['exec']} sl_hits={a['sl']}")

    # по периодам для лучших
    print("\n=== По периодам: cfg x gate ===")
    for (cfg, gate_s) in sorted(agg, key=lambda kv: (kv[0], str(kv[1]))):
        gate = int(gate_s)
        sub = [r for r in rows if r["cfg"] == cfg and r["gate"] == gate_s]
        bp = defaultdict(list)
        for r in sub:
            bp[r["period"]].append(r)
        parts = []
        for p in sorted(bp):
            items = bp[p]
            pnl = sum(float(x["pnl"]) for x in items)
            wins = sum(1 for x in items if float(x["pnl"]) > 0)
            parts.append(f"{p.split('_')[0]}:{pnl:.0f}({wins}/{len(items)})")
        label = f"gate={gate}" if gate < 99 else "no-gate"
        print(f"  {cfg} {label}: {' | '.join(parts)}")


if __name__ == "__main__":
    main()
