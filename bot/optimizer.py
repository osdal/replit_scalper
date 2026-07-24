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


# ── Scoring ──────────────────────────────────────────────────────────────

def score(stats: BacktestStats) -> float:
    if stats.total_trades < MIN_TRADES:
        return 0.0

    gross_profit = sum(t.pnl for t in stats.trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in stats.trades if t.pnl < 0))

    if gross_loss == 0 and gross_profit > 0:
        return gross_profit * (stats.total_trades ** 0.5)

    if gross_loss == 0:
        return 0.0

    profit_factor = gross_profit / gross_loss
    win_rate = stats.win_rate / 100.0
    dd_penalty = 1.0 / (1.0 + stats.max_drawdown / 100.0)

    return profit_factor * (stats.total_trades ** 0.5) * win_rate * dd_penalty


# ── Trial config builder ─────────────────────────────────────────────────

def build_trial_params(trial: optuna.Trial, base_cfg: Config) -> dict:
    p = {}

    p["ema_fast"] = trial.suggest_int("ema_fast", 5, 20)
    p["ema_slow"] = trial.suggest_int("ema_slow", p["ema_fast"] + 3, 55)
    p["sl_pct"] = trial.suggest_float("sl_pct", 0.2, 1.5, step=0.05)
    p["tp1_pct"] = trial.suggest_float("tp1_pct", 0.2, 1.0, step=0.05)
    tp2_min = round(p["tp1_pct"] + 0.1, 2)
    tp2_min_aligned = max(0.3, round(tp2_min * 10) / 10)
    p["tp2_pct"] = trial.suggest_float("tp2_pct", tp2_min_aligned, 2.4, step=0.1)
    p["volume_multiplier"] = trial.suggest_float("volume_multiplier", 1.0, 2.5, step=0.1)
    p["tp1_close_pct"] = trial.suggest_int("tp1_close_pct", 30, 70, step=10)
    p["risk_pct"] = trial.suggest_float("risk_pct", 1.0, 10.0, step=0.5)

    htf_fast = trial.suggest_int("htf_ema_fast", 5, 15)
    p["htf_ema_fast"] = htf_fast
    p["htf_ema_slow"] = trial.suggest_int("htf_ema_slow", htf_fast + 3, 40)

    return p


def apply_trial_params(cfg: Config, params: dict) -> Config:
    cfg = deepcopy(cfg)
    for k, v in params.items():
        setattr(cfg, k, v)
    cfg.htf_enabled = True
    cfg.mode = "backtest"
    return cfg


# ── Objective builder (with HTF caching + pruning support) ───────────────

def build_objective(base_cfg: Config, df_raw: pd.DataFrame, df_htf: pd.DataFrame | None = None):
    from strategy import calculate_htf_indicators
    silent_log = logging.getLogger("optuna_trial")
    silent_log.setLevel(logging.CRITICAL)

    htf_indicators_cache: dict[str, pd.DataFrame] = {}

    def objective(trial: optuna.Trial) -> float:
        params = build_trial_params(trial, base_cfg)
        cfg = apply_trial_params(base_cfg, params)

        # HTF caching: keyed by htf_ema_fast/htf_ema_slow
        precomputed_htf = None
        if cfg.htf_enabled and df_htf is not None:
            htf_key = f"{cfg.htf_ema_fast}_{cfg.htf_ema_slow}"
            if htf_key not in htf_indicators_cache:
                htf_indicators_cache[htf_key] = calculate_htf_indicators(df_htf.copy(), cfg)
            precomputed_htf = htf_indicators_cache[htf_key]

        stats = asyncio.run(run_backtest_on_df(df_raw.copy(), cfg, silent_log, df_htf=precomputed_htf))
        trial.set_user_attr("total_trades", stats.total_trades)
        trial.set_user_attr("win_rate", round(stats.win_rate, 1))
        trial.set_user_attr("total_pnl", round(stats.total_pnl, 2))
        trial.set_user_attr("max_drawdown", round(stats.max_drawdown, 2))
        trial.set_user_attr("avg_win", round(stats.avg_win, 2))
        trial.set_user_attr("avg_loss", round(stats.avg_loss, 2))
        trial.set_user_attr("score", round(score(stats), 4))

        return trial.user_attrs["score"]

    return objective


# ── Output helpers ───────────────────────────────────────────────────────

def print_top(study: optuna.Study, n: int = 10) -> None:
    trials = [t for t in study.trials if t.value is not None and t.value > 0]
    trials.sort(key=lambda t: t.value, reverse=True)

    if not trials:
        print("\n  No profitable combinations found.")
        print("  Try: longer date range, different symbol/timeframe, or more trials.")
        return

    best = trials[0]
    print(f"\n{'=' * 108}")
    print(f"  TOP {min(n, len(trials))} RESULTS  (out of {len(trials)} profitable trials)")
    print(f"{'=' * 108}")
    print(f"  Best params:")
    for k, v in best.params.items():
        if isinstance(v, float):
            print(f"    {k:25s} = {v:.4f}".rstrip("0").rstrip("."))
        else:
            print(f"    {k:25s} = {v}")
    print(f"  Best score: {best.value:.4f}")
    print(f"{'=' * 108}")
    has_htf = any("htf_ema_fast" in t.params for t in trials)
    if has_htf:
        header = (
            f"  {'Rank':>4}  {'Score':>8}  {'Trades':>6}  {'WR%':>5}"
            f"  {'PnL':>8}  {'DD%':>6}  {'EMA_F':>5}  {'EMA_S':>5}"
            f"  {'SL%':>5}  {'TP1%':>5}  {'TP2%':>5}  {'VolX':>5}  {'TP1cl%':>6}  {'Risk%':>6}"
            f"  {'HTF_F':>5}  {'HTF_S':>5}"
        )
    else:
        header = (
            f"  {'Rank':>4}  {'Score':>8}  {'Trades':>6}  {'WR%':>5}"
            f"  {'PnL':>8}  {'DD%':>6}  {'EMA_F':>5}  {'EMA_S':>5}"
            f"  {'SL%':>5}  {'TP1%':>5}  {'TP2%':>5}  {'VolX':>5}  {'TP1cl%':>6}  {'Risk%':>6}"
        )
    print(header)
    hr_len = len(header) - 2
    print(f"  {'-' * hr_len}")
    for rank, t in enumerate(trials[:n], start=1):
        p = t.params
        attrs = t.user_attrs
        tp1_display = f"{p.get('tp1_pct', 0):.2f}"
        tp2_display = f"{p.get('tp2_pct', 0):.2f}"
        sl_display = f"{p.get('sl_pct', 0):.2f}"
        risk_display = f"{p.get('risk_pct', 0):.1f}"
        vol_display = f"{p.get('volume_multiplier', 0):.1f}"
        htf_f = p.get('htf_ema_fast')
        htf_s = p.get('htf_ema_slow')
        htf_part = f"  {htf_f:>5}  {htf_s:>5}" if htf_f is not None else ""
        print(
            f"  {rank:>4}  {t.value:>8.3f}  "
            f"{attrs.get('total_trades', '-'):>6}  {attrs.get('win_rate', '-'):>5}  "
            f"{attrs.get('total_pnl', '-'):>8}  {attrs.get('max_drawdown', '-'):>6}  "
            f"{p.get('ema_fast', '-'):>5}  {p.get('ema_slow', '-'):>5}  "
            f"{sl_display:>5}  {tp1_display:>5}  {tp2_display:>5}  {vol_display:>5}  "
            f"{p.get('tp1_close_pct', '-'):>6}  {risk_display:>6}"
            f"{htf_part}"
        )
    print(f"{'=' * 108}")


def save_csv(study: optuna.Study, out_path: str) -> None:
    trials = [t for t in study.trials if t.value is not None and t.value > 0]
    trials.sort(key=lambda t: t.value, reverse=True)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        if not trials:
            f.write("No profitable trials found.\n")
            return
        fieldnames = ["rank", "score", "total_trades", "win_rate", "total_pnl",
                       "max_drawdown", "avg_win", "avg_loss"] + list(trials[0].params.keys())
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for rank, t in enumerate(trials[:100], start=1):
            attrs = t.user_attrs
            row = {
                "rank": rank,
                "score": f"{t.value:.4f}",
                "total_trades": attrs.get("total_trades", ""),
                "win_rate": attrs.get("win_rate", ""),
                "total_pnl": attrs.get("total_pnl", ""),
                "max_drawdown": attrs.get("max_drawdown", ""),
                "avg_win": attrs.get("avg_win", ""),
                "avg_loss": attrs.get("avg_loss", ""),
            }
            row.update(t.params)
            writer.writerow(row)

    print(f"  Full results (top 100) saved -> {out_path}")


# ── Data download ────────────────────────────────────────────────────────

async def download_data(cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame | None]:
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
        df_htf = None
        if cfg.htf_enabled:
            df_htf = await get_historical_klines(
                client=client,
                symbol=cfg.symbol,
                interval=cfg.htf_timeframe,
                start=cfg.backtest_start,
                end=cfg.backtest_end,
            )
        return df, df_htf
    finally:
        await client.close_connection()


def estimate_time(n_trials: int, n_candles: int) -> str:
    ms_per_candle = 0.01
    sec_per_trial = max(0.05, n_candles * ms_per_candle / 1000)
    total_sec = sec_per_trial * n_trials
    if total_sec < 60:
        return f"~{int(total_sec)} seconds"
    return f"~{total_sec / 60:.1f} minutes"


# ── CLI performance hint ─────────────────────────────────────────────────

def _print_perf_hint(cpu_count: int) -> None:
    if cpu_count > 1:
        print(f"  CPU cores: {cpu_count}. Use --jobs {cpu_count} for maximum speed.")


# ── Main ─────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Optimize scalper parameters using Optuna",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python optimizer.py
  python optimizer.py --symbol ETHUSDT --timeframe 15m --start 2024-01-01 --end 2024-06-01
  python optimizer.py --trials 300 --jobs 8
  python optimizer.py --symbol BTCUSDT --timeframe 1m --start 2024-03-01 --end 2024-04-01
        """,
    )
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    parser.add_argument("--symbol", default=None, help="Trading pair, e.g. BTCUSDT (overrides config)")
    parser.add_argument("--timeframe", default=None, help="Candle timeframe, e.g. 1m 5m 15m (overrides config)")
    parser.add_argument("--start", default=None, help="Backtest start date YYYY-MM-DD (overrides config)")
    parser.add_argument("--end", default=None, help="Backtest end date YYYY-MM-DD (overrides config)")
    parser.add_argument("--trials", type=int, default=100, help="Number of Optuna trials (default: 100)")
    parser.add_argument("--jobs", type=int, default=1, help="Parallel workers (default: 1, serial)")
    parser.add_argument("--study-name", default=None, help="Optuna study name for SQLite persistence")
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

    cpu = os.cpu_count() or 1
    _print_perf_hint(cpu)
    print(f"\n  Symbol:    {cfg.symbol}")
    print(f"  Timeframe: {cfg.timeframe}")
    print(f"  Period:    {cfg.backtest_start} -> {cfg.backtest_end}")
    print(f"  Trials:    {args.trials}")
    if args.jobs > 1:
        print(f"  Jobs:      {args.jobs} (parallel)")
    print(f"\n  Downloading historical data from Binance...")

    df_raw, df_htf = asyncio.run(download_data(cfg))
    n_candles = len(df_raw)
    htf_info = f" + {len(df_htf)} HTF candles ({cfg.htf_timeframe})" if df_htf is not None else ""
    print(f"  Downloaded {n_candles} candles{htf_info}. Data will be reused across all trials.")
    print(f"  Estimated time: {estimate_time(args.trials * args.jobs, n_candles)}")
    print(f"\n  Running optimization...\n")

    # SQLite persistence if --study-name given
    storage = None
    if args.study_name:
        db_dir = os.path.join(os.path.dirname(__file__), "..", "data")
        os.makedirs(db_dir, exist_ok=True)
        db_path = os.path.join(db_dir, "optuna.db")
        storage = optuna.storages.RDBStorage(f"sqlite:///{db_path}")
        load_if_exists = True
    else:
        storage = None
        load_if_exists = False

    study_name = args.study_name or "scalper_opt"

    study = optuna.create_study(
        direction="maximize",
        study_name=study_name,
        storage=storage,
        load_if_exists=load_if_exists,
        sampler=optuna.samplers.TPESampler(seed=42),
    )

    completed = [0]
    initial_trials = len(study.trials)

    def progress_callback(study, trial) -> None:
        completed[0] += 1
        n = completed[0] + initial_trials
        best = study.best_value if study.best_value is not None else 0.0
        bar_len = 30
        filled = int(bar_len * n / (args.trials + initial_trials))
        bar = "#" * filled + "-" * (bar_len - filled)
        print(f"\r  [{bar}] {n}/{args.trials + initial_trials}  best={best:.4f}", end="", flush=True)

    study.optimize(
        build_objective(cfg, df_raw, df_htf=df_htf),
        n_trials=args.trials,
        n_jobs=args.jobs,
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
        f"{cfg.backtest_start}->{cfg.backtest_end} | "
        f"trials={args.trials} | best={study.best_value:.4f}"
    )


if __name__ == "__main__":
    main()
