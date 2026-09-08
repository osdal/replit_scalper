#!/usr/bin/env python3
"""
Download history for multiple timeframes (1m,5m,15m,30m,1h) for every symbol
(from config_*.yaml) and run Optuna optimisation with the constraint:
stop‑loss ≤ take‑profit.
Results are saved to logs/optuna_month_tf_<timestamp>.csv.
The script runs independently of the live bot – it only reads market data
and writes log files.
"""

import argparse
import asyncio
import csv
import datetime
import logging
import os
import sys
from copy import deepcopy

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
    if stats.total_trades < MIN_TRADES:
        return 0.0
    gross_profit = sum(t.pnl for t in stats.trades if t.pnl > 0)
    gross_loss = abs(sum(t.pnl for t in stats.trades if t.pnl < 0))
    if gross_loss == 0:
        return gross_profit * (stats.total_trades ** 0.5) if gross_profit > 0 else 0.0
    profit_factor = gross_profit / gross_loss
    dd_penalty = 1.0 / (1.0 + stats.max_drawdown / 100.0)
    return profit_factor * (stats.total_trades ** 0.5) * (stats.win_rate / 100.0) * dd_penalty

def build_trial_params(trial: optuna.Trial, base_cfg: Config) -> dict:
    p = {}
    # EMA
    p["ema_fast"] = trial.suggest_int("ema_fast", 5, 30)
    p["ema_slow"] = trial.suggest_int("ema_slow", p["ema_fast"] + 3, 60)

    # SL / TP with constraint SL <= TP1 <= TP2
    sl = trial.suggest_float("sl_pct", 0.1, 2.0, step=0.05)
    tp1_min = sl  # TP1 must be at least SL
    tp1 = trial.suggest_float("tp1_pct", tp1_min, 3.0, step=0.05)
    tp2_min = tp1  # TP2 must be at least TP1
    tp2 = trial.suggest_float("tp2_pct", tp2_min, 5.0, step=0.05)

    p["sl_pct"] = sl
    p["tp1_pct"] = tp1
    p["tp2_pct"] = tp2
    p["tp1_close_pct"] = trial.suggest_int("tp1_close_pct", 30, 100, step=10)

    # risk & volume
    p["risk_pct"] = 3.0
    p["volume_multiplier"] = trial.suggest_float("volume_multiplier", 1.0, 3.0, step=0.1)

    # HTF (keep enabled from base config)
    p["htf_enabled"] = base_cfg.htf_enabled
    p["htf_ema_fast"] = trial.suggest_int("htf_ema_fast", 5, 20)
    p["htf_ema_slow"] = trial.suggest_int("htf_ema_slow", p["htf_ema_fast"] + 3, 40)

    return p

def apply_trial_params(cfg: Config, params: dict) -> Config:
    cfg = deepcopy(cfg)
    for k, v in params.items():
        setattr(cfg, k, v)
    if "risk_pct" not in params:
        cfg.risk_pct = 3.0
    cfg.mode = "backtest"
    return cfg

def build_objective(base_cfg: Config, df_raw: pd.DataFrame, df_htf: pd.DataFrame | None = None):
    from strategy import calculate_htf_indicators
    silent_log = logging.getLogger("optuna_trial")
    silent_log.setLevel(logging.CRITICAL)

    htf_cache = {}

    def objective(trial: optuna.Trial) -> float:
        params = build_trial_params(trial, base_cfg)
        cfg = apply_trial_params(base_cfg, params)

        # HTF caching
        precomputed_htf = None
        if cfg.htf_enabled and df_htf is not None:
            htf_key = f"{cfg.htf_ema_fast}_{cfg.htf_ema_slow}"
            if htf_key not in htf_cache:
                htf_cache[htf_key] = calculate_htf_indicators(df_htf.copy(), cfg)
            precomputed_htf = htf_cache[htf_key]

        stats = run_backtest_on_df(df_raw.copy(), cfg, silent_log, df_htf=precomputed_htf)
        trial.set_user_attr("total_trades", stats.total_trades)
        trial.set_user_attr("win_rate", round(stats.win_rate, 1))
        trial.set_user_attr("total_pnl", round(stats.total_pnl, 2))
        trial.set_user_attr("max_drawdown", round(stats.max_drawdown, 2))
        trial.set_user_attr("score", round(score(stats), 4))
        return trial.user_attrs["score"]

    return objective

async def download_data(cfg: Config):
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

def get_symbol_list():
    bot_dir = os.path.join(os.path.dirname(__file__))
    symbols = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            symbols.append(sym)
    return sorted(set(symbols))

def main():
    parser = argparse.ArgumentParser(
        description="Multi‑timeframe history download + Optuna optimisation (SL ≤ TP)"
    )
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--trials", type=int, default=30, help="Trials per symbol per timeframe")
    parser.add_argument("--jobs", type=int, default=1, help="Parallel workers (default: 1)")
    parser.add_argument("--study-name", default=None, help="Optuna study name (for SQLite)")
    args = parser.parse_args()

    # Basic date sanity
    try:
        datetime.datetime.strptime(args.start, "%Y-%m-%d")
        datetime.datetime.strptime(args.end, "%Y-%m-%d")
    except ValueError:
        sys.exit("Start/end must be YYYY-MM-DD")

    timeframes = ["1m", "5m", "15m", "30m", "1h"]
    symbols = get_symbol_list()
    print(f"Found {len(symbols)} symbols from config_*.yaml")
    print(f"Timeframes to test: {timeframes}")
    print(f"Period: {args.start} -> {args.end}")
    print(f"Trials per symbol per timeframe: {args.trials}")
    print(f"Jobs: {args.jobs}")

    # Prepare log file for overall progress
    log_file = os.path.join(os.path.dirname(__file__), "logs", f"optuna_month_tf_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("optuna_month_tf")
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(logging.StreamHandler(sys.stdout))

    all_results = []

    for sym in symbols:
        for tf in timeframes:
            logger.info(f"=== Processing {sym} [{tf}] ===")
            print(f"\n=== Processing {sym} [{tf}] ===")

            # Load a base config (template) – we will overwrite symbol/dates/timeframe
            base_cfg = load_config(os.path.join(os.path.dirname(__file__), "config.yaml"))
            base_cfg.symbol = sym
            base_cfg.backtest_start = args.start
            base_cfg.backtest_end = args.end
            base_cfg.timeframe = tf   # override timeframe
            base_cfg.mode = "backtest"

            print(f"  Downloading historical data for timeframe {tf} ...")
            logger.info(f"Downloading data for {sym} timeframe {tf}")
            df_raw, df_htf = asyncio.run(download_data(base_cfg))
            print(f"  {len(df_raw)} candles ({tf})")
            if df_htf is not None:
                print(f"  {len(df_htf)} HTF candles ({base_cfg.htf_timeframe})")
            logger.info(f"Downloaded {len(df_raw)} candles + {len(df_htf) if df_htf is not None else 0} HTF")

            study_name = args.study_name or f"month_opt_{sym}_{tf}"
            storage = None
            if args.study_name:
                db_dir = os.path.join(os.path.dirname(__file__), "..", "data")
                os.makedirs(db_dir, exist_ok=True)
                db_path = os.path.join(db_dir, "optuna.db")
                storage = optuna.storages.RDBStorage(f"sqlite:///{db_path}")

            study = optuna.create_study(
                direction="maximize",
                study_name=study_name,
                storage=storage,
                load_if_exists=bool(storage),
                sampler=optuna.samplers.TPESampler(seed=42),
            )

            def progress_callback(study, trial):
                completed = len(study.trials)
                best = study.best_value if study.best_value is not None else 0.0
                if completed % 5 == 0 or completed == args.trials:
                    msg = f"  [{sym} {tf}] trial {completed}/{args.trials} – best={best:.4f}"
                    print(msg)
                    logger.info(msg)

            study.optimize(
                build_objective(base_cfg, df_raw, df_htf=df_htf),
                n_trials=args.trials,
                n_jobs=args.jobs,
                callbacks=[progress_callback],
                show_progress_bar=False,
            )

            # Save best trial info
            best = study.best_trial
            result = {
                "symbol": sym,
                "timeframe": tf,
                "score": round(best.value, 4) if best.value else 0.0,
                "ema_fast": best.params.get("ema_fast", "-"),
                "ema_slow": best.params.get("ema_slow", "-"),
                "sl_pct": round(best.params.get("sl_pct", 0), 4),
                "tp1_pct": round(best.params.get("tp1_pct", 0), 4),
                "tp2_pct": round(best.params.get("tp2_pct", 0), 4),
                "tp1_close_pct": best.params.get("tp1_close_pct", "-"),
                "volume_multiplier": round(best.params.get("volume_multiplier", 0), 2),
                "htf_ema_fast": best.params.get("htf_ema_fast", "-"),
                "htf_ema_slow": best.params.get("htf_ema_slow", "-"),
                "trials": args.trials,
                "best_trades": best.user_attrs.get("total_trades", 0),
                "best_win_rate": best.user_attrs.get("win_rate", 0.0),
                "best_pnl": best.user_attrs.get("total_pnl", 0.0),
                "best_dd": best.user_attrs.get("max_drawdown", 0.0),
            }
            all_results.append(result)
            print(f"  Best score for {sym} [{tf}]: {result['score']}")
            logger.info(f"Finished {sym} [{tf}]: {result}")

    # Write CSV summary
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = os.path.join(os.path.dirname(__file__), "logs", f"optuna_month_tf_{timestamp}.csv")
    if all_results:
        fieldnames = list(all_results[0].keys())
        with open(out_csv, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(all_results)
        print(f"\nAll results saved -> {out_csv}")
        logger.info(f"All results saved to {out_csv}")
    else:
        print("\nNo results to save.")
        logger.warning("No results obtained.")

if __name__ == "__main__":
    main()