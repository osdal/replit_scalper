"""
Запуск:
    python signal_overlap.py config_eth.yaml config_sol.yaml config_btc.yaml

Для каждой пары загружает исторические данные, прогоняет стратегию
и выводит таблицу сигналов + отчёт о пересечениях по времени.
"""
import asyncio
import os
import sys
from typing import List, Optional, Dict
from dataclasses import dataclass

import pandas as pd
from binance import AsyncClient
from dotenv import load_dotenv

from config import load_config, Config
from market_data import get_historical_klines
from strategy import calculate_indicators, calculate_htf_indicators, get_signal, get_htf_trend, Signal

load_dotenv()

OVERLAP_MINUTES = 5  # считать сигналы пересекающимися если они в пределах N минут


@dataclass
class SymbolSignal:
    symbol: str
    direction: str
    time: pd.Timestamp
    entry_price: float


async def collect_signals(cfg: Config, client: AsyncClient) -> List[SymbolSignal]:
    df = await get_historical_klines(
        client=client,
        symbol=cfg.symbol,
        interval=cfg.timeframe,
        start=cfg.backtest_start,
        end=cfg.backtest_end,
    )
    df = calculate_indicators(df, cfg)
    df.dropna(inplace=True)

    df_htf: Optional[pd.DataFrame] = None
    if cfg.htf_enabled:
        df_htf = await get_historical_klines(
            client=client,
            symbol=cfg.symbol,
            interval=cfg.htf_timeframe,
            start=cfg.backtest_start,
            end=cfg.backtest_end,
        )
        df_htf = calculate_htf_indicators(df_htf, cfg)

    signals: List[SymbolSignal] = []
    for i in range(1, len(df)):
        window = df.iloc[: i + 1]
        current_time = df.index[i]

        htf_trend = None
        if cfg.htf_enabled and df_htf is not None:
            htf_trend = get_htf_trend(df_htf, current_time)

        signal = get_signal(window, cfg, htf_trend=htf_trend)
        if signal:
            signals.append(SymbolSignal(
                symbol=cfg.symbol,
                direction=signal.direction,
                time=current_time,
                entry_price=signal.entry_price,
            ))

    print(f"  {cfg.symbol}: {len(signals)} signals ({cfg.backtest_start} → {cfg.backtest_end})")
    return signals


def find_overlaps(all_signals: Dict[str, List[SymbolSignal]]) -> pd.DataFrame:
    """Находит временные пересечения сигналов между парами."""
    symbols = list(all_signals.keys())
    rows = []

    for i, sym_a in enumerate(symbols):
        for sym_b in symbols[i + 1:]:
            sigs_a = all_signals[sym_a]
            sigs_b = all_signals[sym_b]
            overlaps = 0
            total = min(len(sigs_a), len(sigs_b))

            for sa in sigs_a:
                for sb in sigs_b:
                    diff = abs((sa.time - sb.time).total_seconds()) / 60
                    if diff <= OVERLAP_MINUTES:
                        overlaps += 1
                        break  # считаем один раз на сигнал sym_a

            pct = overlaps / len(sigs_a) * 100 if sigs_a else 0
            rows.append({
                "Pair A": sym_a,
                "Pair B": sym_b,
                "Signals A": len(sigs_a),
                "Signals B": len(sigs_b),
                "Overlaps": overlaps,
                "Overlap %": f"{pct:.1f}%",
            })

    return pd.DataFrame(rows)


def print_signal_table(all_signals: Dict[str, List[SymbolSignal]]) -> None:
    """Выводит все сигналы по парам в хронологическом порядке."""
    rows = []
    for sym, sigs in all_signals.items():
        for s in sigs:
            rows.append({"time": s.time, "symbol": sym, "direction": s.direction, "price": s.entry_price})
    if not rows:
        return
    df = pd.DataFrame(rows).sort_values("time")
    df["time"] = df["time"].dt.strftime("%Y-%m-%d %H:%M")
    print("\n=== ALL SIGNALS (chronological) ===")
    print(df.to_string(index=False))


async def main():
    config_paths = sys.argv[1:] if len(sys.argv) > 1 else ["config.yaml"]
    if len(config_paths) < 2:
        print("Usage: python signal_overlap.py config_eth.yaml config_sol.yaml [config_btc.yaml ...]")
        print("Need at least 2 config files to compare.")
        sys.exit(1)

    api_key    = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")

    client = await AsyncClient.create(
        api_key=api_key or None,
        api_secret=api_secret or None,
    )

    try:
        all_signals: Dict[str, List[SymbolSignal]] = {}
        print("\nCollecting signals...")
        for path in config_paths:
            cfg = load_config(path)
            sigs = await collect_signals(cfg, client)
            all_signals[cfg.symbol] = sigs

        print_signal_table(all_signals)

        print("\n=== OVERLAP REPORT ===")
        df_overlaps = find_overlaps(all_signals)
        print(df_overlaps.to_string(index=False))
        print(f"\n(Overlap window: ±{OVERLAP_MINUTES} min)")

    finally:
        await client.close_connection()


if __name__ == "__main__":
    asyncio.run(main())