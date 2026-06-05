"""
Optuna-based parameter optimizer for the scalper bot.

Usage (from bot/ directory):
    python optimizer.py
    python optimizer.py --trials 200 --jobs 1

Results are saved to logs/optimization_<timestamp>.csv
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
from binance import AsyncClient
from dotenv import load_dotenv

from config import load_config, Config
from backtester import run_backtest, BacktestStats
from logger import get_logger

load_dotenv()

optuna.logging.set_verbosity(optuna.logging.WARNING)


def score(stats: BacktestStats) -> float:
    """
    Composite score that balances profitability and trade count.

    - profit_factor = gross_profit / gross_loss  (>1 = profitable)
    - Penalizes strategies with too few trades (< MIN_TRADES → score = 0)
    - score = profit_factor * sqrt(n_trades)
    """
    MIN_TRADES = 10

    if stats.total_trades < MIN_TRADES:
        return 0.0

    gross_profit = sum(t.pnl for t in stats.trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in stats.trades if t.pnl < 0))

    if gross_loss == 0:
        return gross_profit * (stats.total_trades ** 0.5)

    profit_factor = gross_profit / gross_loss
    return profit_factor * (stats.total_trades ** 0.5)


def make_trial_config(base_cfg: Config, trial: optuna.Trial) -> Config:
    """Create a Config variant from Optuna trial suggestions."""
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


async def run_trial(base_cfg: Config, trial: optuna.Trial) -> float:
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")

    client = await AsyncClient.create(
        api_key=api_key or None,
        api_secret=api_secret or None,
    )
    silent_log = logging.getLogger("optuna_trial")
    silent_log.setLevel(logging.CRITICAL)

    try:
        cfg = make_trial_config(base_cfg, trial)
        stats = await run_backtest(cfg, client, silent_log)
        return score(stats)
    finally:
        await client.close_connection()


def objective(base_cfg: Config) -> optuna.ObjectiveFuncType:
    def _objective(trial: optuna.Trial) -> float:
        return asyncio.run(run_trial(base_cfg, trial))
    return _objective


def save_results(study: optuna.Study, out_path: str, base_cfg: Config) -> None:
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
        for rank, t in enumerate(trials[:50], start=1):
            row = {"rank": rank, "score": f"{t.value:.4f}"}
            row.update({k: v for k, v in t.params.items()})
            writer.writerow(row)

    print(f"\nTop results saved → {out_path}")


def print_top(study: optuna.Study, n: int = 10) -> None:
    trials = [t for t in study.trials if t.value is not None and t.value > 0]
    trials.sort(key=lambda t: t.value, reverse=True)

    if not trials:
        print("\nNo profitable parameter combinations found.")
        print("Try: wider backtest period, different symbol, or more trials.")
        return

    print(f"\n{'=' * 70}")
    print(f"  TOP {min(n, len(trials))} RESULTS")
    print(f"{'=' * 70}")
    best = trials[0]
    print(f"  Best score: {best.value:.4f}")
    print(f"  Best params:")
    for k, v in best.params.items():
        print(f"    {k:25s} = {v}")
    print(f"{'=' * 70}")
    print(f"  Rank | Score  | ema_fast | ema_slow | sl_pct | tp1_pct | tp2_pct | vol_mult | tp1_close%")
    print(f"  {'-' * 95}")
    for rank, t in enumerate(trials[:n], start=1):
        p = t.params
        print(
            f"  {rank:4d} | {t.value:6.3f} | "
            f"{p.get('ema_fast', '-'):8} | {p.get('ema_slow', '-'):8} | "
            f"{p.get('sl_pct', '-'):6} | {p.get('tp1_pct', '-'):7} | "
            f"{p.get('tp2_pct', '-'):7} | {p.get('volume_multiplier', '-'):8} | "
            f"{p.get('tp1_close_pct', '-')}"
        )
    print(f"{'=' * 70}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Optimize bot parameters with Optuna")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--trials", type=int, default=100, help="Number of Optuna trials (default: 100)")
    parser.add_argument("--jobs", type=int, default=1, help="Parallel jobs (default: 1)")
    args = parser.parse_args()

    base_cfg = load_config(args.config)
    log = get_logger(base_cfg.log_file, "optimize")

    log.info(
        f"Optimizer starting | {base_cfg.symbol} {base_cfg.timeframe} "
        f"{base_cfg.backtest_start} → {base_cfg.backtest_end} | trials={args.trials}"
    )

    study = optuna.create_study(
        direction="maximize",
        study_name="scalper_optimization",
        sampler=optuna.samplers.TPESampler(seed=42),
    )

    print(f"\nRunning {args.trials} trials on {base_cfg.symbol} {base_cfg.timeframe} "
          f"({base_cfg.backtest_start} → {base_cfg.backtest_end})")
    print("Each trial = full backtest with different parameters. Please wait...\n")

    def progress_callback(study: optuna.Study, trial: optuna.FrozenTrial) -> None:
        n = len(study.trials)
        best = study.best_value if study.best_value is not None else 0.0
        if n % 10 == 0 or n == 1:
            print(f"  Trial {n:4d}/{args.trials} | best score so far: {best:.4f}")

    study.optimize(
        objective(base_cfg),
        n_trials=args.trials,
        n_jobs=args.jobs,
        callbacks=[progress_callback],
        show_progress_bar=False,
    )

    print_top(study)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_path = os.path.join(os.path.dirname(base_cfg.log_file), f"optimization_{timestamp}.csv")
    save_results(study, csv_path, base_cfg)

    log.info(f"Optimization complete | {len(study.trials)} trials | best score={study.best_value:.4f}")


if __name__ == "__main__":
    main()
