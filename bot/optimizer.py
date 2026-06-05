"""
Optuna-based parameter optimizer for the scalper bot.

Historical data is downloaded ONCE, then reused across all trials.

Usage (from bot/ directory):
    python optimizer.py
    python optimizer.py --symbol ETHUSDT --timeframe 15m --start 2024-01-01 --end 2024-06-01
    python optimizer.py --trials 200
    python optimizer.py --help
"""

import argparse
import asyncio
import csv
import logging
import os
import sys
from copy import deepcopy
from datetime import datetime

import optuna
import pandas as pd
from binance import AsyncClient
from dotenv import load_dotenv

from backtester import BacktestStats, run_backtest_on_df
from config import Config, load_config
from logger import get_logger

load_dotenv()
optuna.logging.set_verbosity(optuna.logging.WARNING)

MIN_TRADES = 10


def score(stats: BacktestStats) -> float:
    """
    profit_factor × sqrt(n_trades)
    Balances quality and quantity — prevents single lucky trades from winning.
    Returns 0 if fewer than MIN_TRADES trades.
    """
    if stats.total_trades < MIN_TRADES:
        return 0.0

    gross_profit = sum(t.pnl for t in stats.trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in stats.trades if t.pnl < 0))

    if gross_loss == 0:
        return gross_profit * (stats.total_trades ** 0.5)

    profit_factor = gross_profit / gross_loss
    return profit_factor * (stats.total_trades ** 0.5)


def make_trial_config(base_cfg: Config, trial: optuna.Trial) -> Config:
    ema_fast = trial.suggest_int("ema_fast", 5, 20)
    ema_slow = trial.suggest_int("ema_slow", ema_fast + 3, 55)
    sl_pct = trial.suggest_float("sl_pct", 0.2, 1.5, step=0.05)
    tp1_pct = trial.suggest_float("tp1_pct", 0.2, 1.0, step=0.05)
    tp2_pct = trial.suggest_float("tp2_pct", tp1_pct + 0.1, 2.5, step=0.1)
    volume_multiplier = trial.suggest_float("volume_multiplier", 1.0, 2.5, step=0.1)
    tp1_close_pct = trial.suggest_int("tp1_close_pct", 30, 70, step=10)

    cfg = deepcopy(base_cfg)
    cfg.ema_fast = ema_fast
    cfg.ema_slow = ema_slow
    cfg.sl_pct = sl_pct
    cfg.tp1_pct = tp1_pct
    cfg.tp2_pct = tp2_pct
    cfg.volume_multiplier = volume_multiplier
    cfg.tp1_close_pct = tp1_close_pct
    cfg.mode = "backtest"
    return cfg


def build_objective(base_cfg: Config, df_raw: pd.DataFrame):
    """Returns an Optuna objective that reuses the pre-downloaded DataFrame."""
    silent_log = logging.getLogger("optuna_trial")
    silent_log.setLevel(logging.CRITICAL)

    def objective(trial: optuna.Trial) -> float:
        cfg = make_trial_config(base_cfg, trial)
        stats = asyncio.run(run_backtest_on_df(df_raw.copy(), cfg, silent_log))
        return score(stats)

    return objective


def print_top(study: optuna.Study, n: int = 10) -> None:
    trials = [t for t in study.trials if t.value is not None and t.value > 0]
    trials.sort(key=lambda t: t.value, reverse=True)

    if not trials:
        print("\n  No profitable combinations found.")
        print("  Try: longer date range, different symbol/timeframe, or more trials.")
        return

    best = trials[0]
    print(f"\n{'=' * 72}")
    print(f"  TOP {min(n, len(trials))} RESULTS  (out of {len(trials)} profitable trials)")
    print(f"{'=' * 72}")
    print(f"  Best params:")
    for k, v in best.params.items():
        print(f"    {k:25s} = {v}")
    print(f"  Best score: {best.value:.4f}")
    print(f"{'=' * 72}")
    header = f"  {'Rank':>4}  {'Score':>6}  {'EMA_F':>5}  {'EMA_S':>5}  {'SL%':>5}  {'TP1%':>5}  {'TP2%':>5}  {'Vol×':>5}  {'TP1cl%':>6}"
    print(header)
    print(f"  {'-' * 66}")
    for rank, t in enumerate(trials[:n], start=1):
        p = t.params
        print(
            f"  {rank:>4}  {t.value:>6.3f}  "
            f"{p.get('ema_fast', '-'):>5}  {p.get('ema_slow', '-'):>5}  "
            f"{p.get('sl_pct', '-'):>5}  {p.get('tp1_pct', '-'):>5}  "
            f"{p.get('tp2_pct', '-'):>5}  {p.get('volume_multiplier', '-'):>5}  "
            f"{p.get('tp1_close_pct', '-'):>6}"
        )
    print(f"{'=' * 72}")


def save_csv(study: optuna.Study, out_path: str) -> None:
    trials = [t for t in study.trials if t.value is not None and t.value > 0]
    trials.sort(key=lambda t: t.value, reverse=True)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        if not trials:
            f.write("No profitable trials found.\n")
            return
        fieldnames = ["rank", "score"] + list(trials[0].params.keys())
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rank, t in enumerate(trials[:100], start=1):
            row = {"rank": rank, "score": f"{t.value:.4f}"}
            row.update(t.params)
            writer.writerow(row)

    print(f"  Full results (top 100) saved → {out_path}")


async def download_data(cfg: Config) -> pd.DataFrame:
    from market_data import get_historical_klines
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    try:
        df = await get_historical_klines(
            client=client,
            symbol=cfg.symbol,
            interval=cfg.timeframe,
            start=cfg.backtest_start,
            end=cfg.backtest_end,
        )
        return df
    finally:
        await client.close_connection()


def estimate_time(n_trials: int, n_candles: int) -> str:
    ms_per_candle = 0.01
    sec_per_trial = max(0.05, n_candles * ms_per_candle / 1000)
    total_sec = sec_per_trial * n_trials
    if total_sec < 60:
        return f"~{int(total_sec)} seconds"
    return f"~{total_sec / 60:.1f} minutes"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Optimize scalper parameters using Optuna",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python optimizer.py
  python optimizer.py --symbol ETHUSDT --timeframe 15m --start 2024-01-01 --end 2024-06-01
  python optimizer.py --trials 300 --symbol BTCUSDT --timeframe 1m --start 2024-03-01 --end 2024-04-01
        """,
    )
    parser.add_argument("--config",     default="config.yaml",  help="Path to config.yaml")
    parser.add_argument("--symbol",     default=None,            help="Trading pair, e.g. BTCUSDT (overrides config)")
    parser.add_argument("--timeframe",  default=None,            help="Candle timeframe, e.g. 1m 5m 15m (overrides config)")
    parser.add_argument("--start",      default=None,            help="Backtest start date YYYY-MM-DD (overrides config)")
    parser.add_argument("--end",        default=None,            help="Backtest end date YYYY-MM-DD (overrides config)")
    parser.add_argument("--trials",     type=int, default=100,   help="Number of Optuna trials (default: 100)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.symbol:
        cfg.symbol = args.symbol.upper()
    if args.timeframe:
        cfg.timeframe = args.timeframe
    if args.start:
        cfg.backtest_start = args.start
    if args.end:
        cfg.backtest_end = args.end
    cfg.mode = "backtest"

    log = get_logger(cfg.log_file, "optimize")

    print(f"\n  Symbol:    {cfg.symbol}")
    print(f"  Timeframe: {cfg.timeframe}")
    print(f"  Period:    {cfg.backtest_start}  →  {cfg.backtest_end}")
    print(f"  Trials:    {args.trials}")
    print(f"\n  Downloading historical data from Binance...")

    df_raw = asyncio.run(download_data(cfg))
    n_candles = len(df_raw)
    print(f"  Downloaded {n_candles} candles. Data will be reused across all trials.")
    print(f"  Estimated time: {estimate_time(args.trials, n_candles)}")
    print(f"\n  Running optimization...\n")

    study = optuna.create_study(
        direction="maximize",
        study_name="scalper_opt",
        sampler=optuna.samplers.TPESampler(seed=42),
    )

    completed = [0]

    def progress_callback(study: optuna.Study, trial: optuna.FrozenTrial) -> None:
        completed[0] += 1
        n = completed[0]
        best = study.best_value if study.best_value is not None else 0.0
        bar_len = 30
        filled = int(bar_len * n / args.trials)
        bar = "█" * filled + "░" * (bar_len - filled)
        print(f"\r  [{bar}] {n}/{args.trials}  best={best:.4f}", end="", flush=True)

    study.optimize(
        build_objective(cfg, df_raw),
        n_trials=args.trials,
        n_jobs=1,
        callbacks=[progress_callback],
        show_progress_bar=False,
    )
    print()

    print_top(study, n=10)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(os.path.dirname(cfg.log_file), f"optimization_{timestamp}.csv")
    save_csv(study, csv_path)

    log.info(
        f"Optimization done | {cfg.symbol} {cfg.timeframe} "
        f"{cfg.backtest_start}→{cfg.backtest_end} | "
        f"trials={args.trials} | best={study.best_value:.4f}"
    )


if __name__ == "__main__":
    main()
