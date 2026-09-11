#!/usr/bin/env python3
"""
Настоящий grid-бот на фьючерсах (малое плечо) — честная симуляция.

Механика (в отличие от псевдо-grid C2):
- сетка равноотстоящих уровней между динамическими границами диапазона,
- цена падает на уровень -> покупаем 1 лот (лонг растёт),
- цена растёт на уровень -> продаём 1 лот (лонг падает; в минус не уходим дальше max_short),
- FIFO-закрытие: каждая продажа закрывает самый старый лот -> реализованный PnL = sell-buy,
- комиссия taker 0.05% на каждое исполнение,
- нереализованный PnL открытого инвентаря считается по последней цене.

Маржа: требуется leverage, чтобы покрыть max позицию. Малым плечом ограничиваем размер
позиции относительно капитала (grid-бот не шортит агрессивно).

Диапазон: скользящее окно lookback свечей, границы = min(low)/max(high) за окно, уровни
перестраиваются каждые rebuild_h свечей.

Параметры сетки: n_levels, lookback, rebuild_h, capital, leverage, fee.
"""

import argparse
import asyncio
import csv
import datetime
import os
import sys
from collections import defaultdict, deque

from binance import AsyncClient
from dotenv import load_dotenv

from market_data import get_historical_klines

load_dotenv()

PERIODS = [("2026-03-01", "2026-05-31", "mar_may"), ("2026-06-01", "2026-09-07", "jun_sep")]
DEFAULT_FEE = 0.0005  # taker 0.05%

# Сетка конфигураций grid
GRID_CFGS = [
    {"n_levels": 10, "lookback": 96, "rebuild": 48},
    {"n_levels": 20, "lookback": 96, "rebuild": 48},
    {"n_levels": 10, "lookback": 192, "rebuild": 96},
    {"n_levels": 20, "lookback": 192, "rebuild": 96},
]


def get_symbol_list():
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    symbols = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            symbols.append(sym)
    return sorted(set(symbols))


def run_grid(df, cfg, capital=1000.0, leverage=5.0, fee=DEFAULT_FEE):
    """
    Симуляция grid на фьючерсах. Оперирует qty (доля базового актива).

    Бюджет на всю сетку: capital*leverage (номинал). Делим на n_levels -> qty на лот.
    Покупка на уровне вниз добавляет лот, продажа на уровне вверх закрывает FIFO-лот.
    PnL в USDT. Нереализованный PnL открытых лотов по последней цене.
    """
    n = len(df)
    n_levels = cfg["n_levels"]
    lookback = cfg["lookback"]
    rebuild = cfg["rebuild"]

    lots = deque()          # FIFO: цены покупки открытых лотов
    realized = 0.0
    exec_count = 0
    levels = []
    lo = hi = 0.0

    def build_levels(idx):
        nonlocal lo, hi, levels
        start = max(0, idx - lookback + 1)
        lo = float(df["low"].iloc[start:idx + 1].min())
        hi = float(df["high"].iloc[start:idx + 1].max())
        if hi <= lo:
            return
        step = (hi - lo) / n_levels
        levels = [lo + step * k for k in range(1, n_levels)]

    def lot_qty(ref_price):
        # номинал на лот = (capital*leverage/n_levels) / ref_price
        notional_per_level = capital * leverage / n_levels
        return notional_per_level / ref_price if ref_price else 0.0

    start_idx = min(n - 1, lookback)
    build_levels(start_idx)
    prev_price = float(df["close"].iloc[start_idx])
    ref = (lo + hi) / 2 if hi > lo else prev_price
    qty = lot_qty(ref)  # фикс. qty на лот (масштаб сетки)

    for i in range(start_idx + 1, n):
        h = float(df["high"].iloc[i])
        l = float(df["low"].iloc[i])
        c = float(df["close"].iloc[i])

        if (i - lookback) % rebuild == 0 or not levels:
            build_levels(i)
            ref = (lo + hi) / 2 if hi > lo else prev_price
            qty = lot_qty(ref)

        # Покупки: цена пересекла уровень сверху вниз (low < level <= prev)
        # Лимит: суммарный номинал открытых лотов не больше capital*leverage
        for lv in sorted(levels, reverse=True):
            if lv < prev_price and l <= lv:
                open_notional = sum(b * qty for b in lots) + lv * qty
                if open_notional > capital * leverage:
                    continue  # маржи нет — не докупаем
                lots.append(lv)
                realized -= lv * qty * fee
                exec_count += 1
        # Продажи: цена пересекла уровень снизу вверх (high > level >= prev)
        for lv in sorted(levels):
            if lv > prev_price and h >= lv:
                if lots:
                    buy_px = lots.popleft()
                    pnl = (lv - buy_px) * qty - (lv + buy_px) * qty * fee
                    realized += pnl
                    exec_count += 1
        prev_price = c

    last = float(df["close"].iloc[n - 1])
    unreal = sum((last - b) * qty for b in lots) - sum(b * qty * fee for b in lots)
    total_pnl = realized + unreal
    return total_pnl, total_pnl / capital * 100, exec_count, len(lots)


async def run(symbols, capital, leverage):
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    all_rows = []
    try:
        for si, sym in enumerate(symbols, 1):
            for start, end, pname in PERIODS:
                try:
                    df = await get_historical_klines(client=client, symbol=sym, interval="1h", start=start, end=end)
                    if len(df) < 400:
                        continue
                    for ci, gcfg in enumerate(GRID_CFGS):
                        tag = f"L{gcfg['n_levels']}_lb{gcfg['lookback']}"
                        pnl, pct, n_exec, open_lots = run_grid(df, gcfg, capital, leverage)
                        all_rows.append({
                            "symbol": sym.replace("USDT", ""), "period": f"{start}_{end}",
                            "cfg": tag, "pnl": round(pnl, 2), "pct": round(pct, 2),
                            "exec": n_exec, "open_lots": open_lots,
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
    args = parser.parse_args()
    symbols = get_symbol_list()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    print(f"Symbols: {len(symbols)} | capital={args.capital} lev={args.leverage}x | cfgs={len(GRID_CFGS)} | 2 periods")

    rows = asyncio.run(run(symbols, args.capital, args.leverage))
    if not rows:
        print("Нет данных")
        return
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", f"grid_{ts}.csv")
    fn = list(rows[0].keys())
    with open(out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fn)
        w.writeheader()
        w.writerows(rows)
    print(f"\nSaved -> {out}")

    by_cfg = defaultdict(list)
    for r in rows:
        by_cfg[r["cfg"]].append(r)
    print("\n=== Grid по конфигурациям (сумма PnL по всем символам и периодам) ===")
    for tag in sorted(by_cfg):
        items = by_cfg[tag]
        pnl = sum(float(x["pnl"]) for x in items)
        execs = sum(int(x["exec"]) for x in items)
        # средний % на символ
        avg_pct = sum(float(x["pct"]) for x in items) / len(items)
        print(f"  {tag:<16} символ-периодов={len(items):<4} sum_pnl={pnl:>9.2f} avg_pct={avg_pct:>6.2f}% exec={execs}")


if __name__ == "__main__":
    main()
