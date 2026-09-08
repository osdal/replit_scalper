#!/usr/bin/env python3
"""
Download last month of history for every symbol (from config_*.yaml) and,
for each TIMEFRAME (1m,5m,15m,30m,1h) separately, optimise ONLY the stops
(SL / TP) of every PRESET (from preset_config.py).

Trades from ALL symbols are pooled into a single result per preset, so the
score represents the preset as a whole, not per-coin.

Results are saved to logs/optuna_presets_<timestamp>.csv.
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
from market_data import get_historical_klines
from preset_config import PRESET_CONFIG
from strategy import calculate_indicators, calculate_htf_indicators

load_dotenv()
optuna.logging.set_verbosity(optuna.logging.WARNING)

MIN_TRADES = 10
TIMEFRAMES = ["1m", "5m", "15m", "30m", "1h"]


def real_score(total_pnl: float, total_capital: float,
               total_trades: int, max_dd: float, min_trades: int = MIN_TRADES) -> float:
    """
    Реальная метрика: риск-скорректированная доходность портфеля.

    - Недостаточно сделок -> жёсткий штраф (оптимизатор избегает такой регион).
    - Отрицательный PnL -> отрицательный score (штраф прямо пропорционален убытку).
    - Положительный PnL -> доходность % делённая на (1 + просадка), с фактором
      уверенности по количеству сделок.
    """
    if total_trades < min_trades:
        return -1000.0
    if total_capital <= 0:
        return -1000.0
    ret_pct = total_pnl / total_capital * 100.0
    if ret_pct <= 0:
        return ret_pct  # отрицательный score уводит оптимизатор от убыточных регионов
    dd_penalty = 1.0 / (1.0 + max_dd / 100.0)
    confidence = min(1.0, total_trades / (min_trades * 3))
    return ret_pct * dd_penalty * confidence


def build_objective(symbol_dfs: list[tuple[pd.DataFrame, pd.DataFrame | None]],
                    base_cfg: Config,
                    preset: str,
                    use_fixed_tp_sl: bool = True,
                    min_rr: float = 1.0):
    silent_log = logging.getLogger("optuna_preset_trial")
    silent_log.setLevel(logging.CRITICAL)

    def objective(trial: optuna.Trial) -> float:
        sl = trial.suggest_float("sl", 0.1, 2.0, step=0.05)
        tp = trial.suggest_float("tp", sl * min_rr, 5.0, step=0.05)
        cfg = deepcopy(base_cfg)
        cfg.mode = "backtest"
        cfg.use_fixed_tp_sl = use_fixed_tp_sl
        cfg.preset_sl_pct = sl
        cfg.preset_tp_pct = tp
        cfg.sl_pct = sl
        cfg.tp1_pct = tp
        cfg.tp2_pct = tp

        # Per-symbol backtest: each symbol sized from its own account curve.
        # All trades are merged chronologically into one portfolio equity curve,
        # and the score is computed on that real portfolio result.
        all_trades = []
        for df, df_htf in symbol_dfs:
            stats = run_backtest_on_df(
                df.copy(), cfg, silent_log,
                df_htf=df_htf,
                enabled_presets=[preset],
                precomputed=True,
            )
            all_trades.extend(stats.trades)

        n_symbols = len(symbol_dfs)
        total_capital = cfg.paper_balance * n_symbols
        combined = BacktestStats(trades=all_trades, initial_balance=total_capital)

        agg_score = real_score(
            combined.total_pnl, total_capital,
            combined.total_trades, combined.max_drawdown,
        )

        trial.set_user_attr("total_trades", combined.total_trades)
        trial.set_user_attr("win_rate", round(combined.win_rate, 1))
        trial.set_user_attr("total_pnl", round(combined.total_pnl, 2))
        trial.set_user_attr("max_drawdown", round(combined.max_drawdown, 2))
        trial.set_user_attr("return_pct", round((combined.total_pnl / total_capital * 100) if total_capital else 0.0, 2))
        trial.set_user_attr("score", round(agg_score, 4))
        return agg_score

    return objective


async def download_precomputed(cfg: Config):
    """Скачивает основной df и HTF, считает индикаторы и dropna — один раз."""
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
        df = calculate_indicators(df, cfg)
        df.dropna(inplace=True)

        df_htf = None
        if cfg.htf_enabled:
            df_htf = await get_historical_klines(
                client=client,
                symbol=cfg.symbol,
                interval=cfg.htf_timeframe,
                start=cfg.backtest_start,
                end=cfg.backtest_end,
            )
            df_htf = calculate_htf_indicators(df_htf, cfg)
        return df, df_htf
    finally:
        await client.close_connection()


def get_symbol_list():
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    symbols = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            symbols.append(sym)
    return sorted(set(symbols))


def main():
    parser = argparse.ArgumentParser(
        description="Per-preset stop (SL/TP) optimisation across all symbols, per timeframe"
    )
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--timeframes", default=",".join(TIMEFRAMES),
                        help="Comma-separated timeframes (default: %s)" % ",".join(TIMEFRAMES))
    parser.add_argument("--presets", default=None,
                        help="Comma-separated preset names (default: all from preset_config.py)")
    parser.add_argument("--trials", type=int, default=30, help="Trials per preset per timeframe")
    parser.add_argument("--jobs", type=int, default=1, help="Parallel workers (default: 1)")
    parser.add_argument("--fixed-tp-sl", action="store_true",
                        help="Использовать фиксированные SL/TP (без ATR-динамики). "
                             "Результаты переносимы только на конфиги с use_fixed_tp_sl: true")
    parser.add_argument("--min-rr", type=float, default=1.0,
                        help="Минимальное соотношение TP/SL (по умолчанию 1.0, т.е. TP >= SL). "
                             "Например 2.0 означает TP >= 2*SL")
    args = parser.parse_args()

    try:
        datetime.datetime.strptime(args.start, "%Y-%m-%d")
        datetime.datetime.strptime(args.end, "%Y-%m-%d")
    except ValueError:
        sys.exit("Start/end must be YYYY-MM-DD")

    timeframes = [t.strip() for t in args.timeframes.split(",") if t.strip()]
    presets = list(PRESET_CONFIG.keys())
    if args.presets:
        requested = [p.strip() for p in args.presets.split(",") if p.strip()]
        presets = [p for p in presets if p in requested]
        if not presets:
            sys.exit(f"None of the requested presets exist. Available: {list(PRESET_CONFIG.keys())}")

    symbols = get_symbol_list()
    print(f"Found {len(symbols)} symbols from config_*.yaml")
    print(f"Timeframes to test: {timeframes}")
    print(f"Presets to test: {len(presets)}")
    print(f"Period: {args.start} -> {args.end}")
    print(f"Trials per preset per timeframe: {args.trials}")
    print(f"Jobs: {args.jobs}")

    log_file = os.path.join(os.path.dirname(__file__), "logs", f"optuna_presets_{datetime.datetime.now():%Y%m%d_%H%M%S}.log")
    file_handler = logging.FileHandler(log_file)
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger = logging.getLogger("optuna_presets")
    logger.setLevel(logging.INFO)
    logger.addHandler(file_handler)
    logger.addHandler(logging.StreamHandler(sys.stdout))

    all_results = []

    for tf in timeframes:
        print(f"\n=== Timeframe {tf} ===")
        logger.info(f"=== Timeframe {tf} ===")

        # 1) Скачиваем данные всех символов один раз для этого ТФ
        symbol_dfs: list[tuple[pd.DataFrame, pd.DataFrame | None]] = []
        for sym in symbols:
            base_cfg = load_config(os.path.join(os.path.dirname(__file__), "config.yaml"))
            base_cfg.symbol = sym
            base_cfg.timeframe = tf
            base_cfg.backtest_start = args.start
            base_cfg.backtest_end = args.end
            base_cfg.mode = "backtest"

            df, df_htf = asyncio.run(download_precomputed(base_cfg))
            symbol_dfs.append((df, df_htf))
            print(f"  {sym}: {len(df)} candles")
        print(f"  Data ready for {len(symbol_dfs)} symbols on {tf}")
        logger.info(f"Data ready for {len(symbol_dfs)} symbols on {tf}")

        # 2) Оптимизация стопов для каждого пресета
        for preset in presets:
            study = optuna.create_study(
                direction="maximize",
                study_name=f"preset_{preset}_{tf}",
                sampler=optuna.samplers.TPESampler(seed=42),
            )

            def progress_callback(study, trial):
                completed = len(study.trials)
                best = study.best_value if study.best_value is not None else 0.0
                if completed % 5 == 0 or completed == args.trials:
                    msg = f"  [{preset} {tf}] trial {completed}/{args.trials} – best={best:.4f}"
                    print(msg)
                    logger.info(msg)

            study.optimize(
                build_objective(symbol_dfs, base_cfg, preset,
                                use_fixed_tp_sl=args.fixed_tp_sl,
                                min_rr=args.min_rr),
                n_trials=args.trials,
                n_jobs=args.jobs,
                callbacks=[progress_callback],
                show_progress_bar=False,
            )

            best = study.best_trial
            result = {
                "preset": preset,
                "timeframe": tf,
                "sl": round(best.params.get("sl", 0), 4),
                "tp": round(best.params.get("tp", 0), 4),
                "score": round(best.value, 4) if best.value else 0.0,
                "trades": best.user_attrs.get("total_trades", 0),
                "win_rate": best.user_attrs.get("win_rate", 0.0),
                "pnl": best.user_attrs.get("total_pnl", 0.0),
                "return_pct": best.user_attrs.get("return_pct", 0.0),
                "dd": best.user_attrs.get("max_drawdown", 0.0),
            }
            all_results.append(result)
            print(f"  Best for {preset} [{tf}]: sl={result['sl']} tp={result['tp']} "
                  f"score={result['score']} pnl={result['pnl']} ret={result['return_pct']}% dd={result['dd']}%")
            logger.info(f"Best for {preset} [{tf}]: {result}")

    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    out_csv = os.path.join(os.path.dirname(__file__), "logs", f"optuna_presets_{timestamp}.csv")
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
