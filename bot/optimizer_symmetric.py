"""
Symmetric TP/SL optimizer — one level for both take-profit and stop-loss.

Usage:
    python optimizer_symmetric.py
    python optimizer_symmetric.py --symbols ETHUSDT,BTCUSDT,SOLUSDT --start 2026-07-01 --end 2026-07-28
    python optimizer_symmetric.py --trials 200 --jobs 4
"""

import argparse
import asyncio
import logging
import os
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

MIN_TRADES = 5


# ── Scoring ──────────────────────────────────────────────────────────────

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


# ── Trial params (symmetric SL = TP) ────────────────────────────────────

def build_trial_params_symmetric(trial: optuna.Trial) -> dict:
    p = {}
    p["ema_fast"] = trial.suggest_int("ema_fast", 5, 20)
    p["ema_slow"] = trial.suggest_int("ema_slow", p["ema_fast"] + 3, 55)
    p["sl_pct"] = trial.suggest_float("sl_pct", 0.3, 2.0, step=0.05)
    p["tp1_pct"] = p["sl_pct"]         # symmetric
    p["tp2_pct"] = p["sl_pct"]         # no TP2 — close 100% at TP1 = SL level
    p["tp1_close_pct"] = 100            # full close at TP1
    p["volume_multiplier"] = trial.suggest_float("volume_multiplier", 1.0, 2.5, step=0.1)
    p["risk_pct"] = 3.0
    htf_fast = trial.suggest_int("htf_ema_fast", 5, 15)
    p["htf_ema_fast"] = htf_fast
    p["htf_ema_slow"] = trial.suggest_int("htf_ema_slow", htf_fast + 3, 40)
    return p


def apply_params(cfg: Config, params: dict) -> Config:
    cfg = deepcopy(cfg)
    for k, v in params.items():
        setattr(cfg, k, v)
    cfg.htf_enabled = True
    cfg.mode = "backtest"
    return cfg


# ── Objective ────────────────────────────────────────────────────────────

def build_objective(base_cfg: Config, df_raw: pd.DataFrame, df_htf: pd.DataFrame | None = None):
    from strategy import calculate_htf_indicators
    silent_log = logging.getLogger("optuna_trial")
    silent_log.setLevel(logging.CRITICAL)
    htf_cache = {}

    def objective(trial: optuna.Trial) -> float:
        params = build_trial_params_symmetric(trial)
        cfg = apply_params(base_cfg, params)
        precomputed_htf = None
        if cfg.htf_enabled and df_htf is not None:
            key = f"{cfg.htf_ema_fast}_{cfg.htf_ema_slow}"
            if key not in htf_cache:
                htf_cache[key] = calculate_htf_indicators(df_htf.copy(), cfg)
            precomputed_htf = htf_cache[key]
        stats = asyncio.run(run_backtest_on_df(df_raw.copy(), cfg, silent_log, df_htf=precomputed_htf))
        trial.set_user_attr("total_trades", stats.total_trades)
        trial.set_user_attr("win_rate", round(stats.win_rate, 1))
        trial.set_user_attr("total_pnl", round(stats.total_pnl, 2))
        trial.set_user_attr("max_drawdown", round(stats.max_drawdown, 2))
        return score(stats)

    return objective


# ── Data ─────────────────────────────────────────────────────────────────

async def download_data(cfg: Config) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    from market_data import get_historical_klines
    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)
    try:
        df = await get_historical_klines(client=client, symbol=cfg.symbol, interval=cfg.timeframe,
                                          start=cfg.backtest_start, end=cfg.backtest_end)
        df_htf = await get_historical_klines(client=client, symbol=cfg.symbol, interval=cfg.htf_timeframe,
                                              start=cfg.backtest_start, end=cfg.backtest_end) if cfg.htf_enabled else None
        return df, df_htf
    finally:
        await client.close_connection()


# ── Single symbol optimization ───────────────────────────────────────────

def optimize_symbol(sym: str, base_cfg_path: str, args) -> dict:
    cfg = load_config(base_cfg_path)
    if sym:
        cfg.symbol = sym.upper()
    cfg.backtest_start = args.start
    cfg.backtest_end = args.end
    cfg.mode = "backtest"

    print(f"\n  {cfg.symbol} ...", end="", flush=True)
    df_raw, df_htf = asyncio.run(download_data(cfg))
    n_candles = len(df_raw)
    print(f" {n_candles} candles", end="", flush=True)

    study_name = f"sym_opt_{cfg.symbol}"
    study = optuna.create_study(
        direction="maximize", study_name=study_name,
        sampler=optuna.samplers.TPESampler(seed=42),
    )

    study.optimize(
        build_objective(cfg, df_raw, df_htf=df_htf),
        n_trials=args.trials, n_jobs=args.jobs,
        show_progress_bar=False,
    )

    best = study.best_trial
    result = {
        "symbol": cfg.symbol,
        "score": round(best.value, 3) if best.value else 0,
        "ema_fast": best.params.get("ema_fast", "-"),
        "ema_slow": best.params.get("ema_slow", "-"),
        "sl_tp_pct": round(best.params.get("sl_pct", 0), 2),
        "vol_mult": round(best.params.get("volume_multiplier", 0), 1),
        "htf_f": best.params.get("htf_ema_fast", "-"),
        "htf_s": best.params.get("htf_ema_slow", "-"),
        "trades": best.user_attrs.get("total_trades", "-"),
        "win_rate": best.user_attrs.get("win_rate", "-"),
        "pnl": best.user_attrs.get("total_pnl", "-"),
        "dd": best.user_attrs.get("max_drawdown", "-"),
    }
    print(f" OK score={result['score']}", flush=True)
    return result


# ── Auto-detect symbols ──────────────────────────────────────────────────

def autodetect_symbols() -> list[str]:
    bot_dir = os.path.join(os.path.dirname(__file__))
    syms = []
    for f in os.listdir(bot_dir):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            # Fix for 1000PEPEUSDT
            if sym == "1000PEPEUSDT":
                syms.append(sym)
            elif not any(c.isdigit() for c in sym):
                syms.append(sym)
    return sorted(set(syms))


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Symmetric TP/SL optimizer for multiple symbols")
    parser.add_argument("--symbols", default=None, help="Comma-separated symbols, e.g. ETHUSDT,BTCUSDT")
    parser.add_argument("--config", default="config.yaml", help="Base config template")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="End date YYYY-MM-DD")
    parser.add_argument("--trials", type=int, default=100, help="Optuna trials per symbol")
    parser.add_argument("--jobs", type=int, default=1, help="Parallel workers")
    args = parser.parse_args()

    if not args.start or not args.end:
        print("  --start and --end are required")
        return

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",")]
    else:
        symbols = autodetect_symbols()

    cpu = os.cpu_count() or 1
    if cpu > 1 and args.jobs == 1:
        print(f"  CPU cores: {cpu}. Use --jobs {cpu} for maximum speed.\n")

    print(f"  Symmetric TP/SL optimizer")
    print(f"  Period: {args.start} -> {args.end}")
    print(f"  Trials per symbol: {args.trials}")
    print(f"  Symbols: {len(symbols)}\n")

    results = []
    for sym in symbols:
        res = optimize_symbol(sym, args.config, args)
        results.append(res)

    # Summary table
    print(f"\n{'=' * 110}")
    header = (
        f"  {'Symbol':>14s}  {'Score':>8}  {'Trades':>6}  {'WR%':>5}"
        f"  {'PnL':>8}  {'DD%':>6}  {'EMA_F':>5}  {'EMA_S':>5}"
        f"  {'SL=TP%':>6}  {'VolX':>5}  {'HTF_F':>5}  {'HTF_S':>5}"
    )
    print(header)
    print(f"  {'-' * 96}")
    results.sort(key=lambda r: r["score"], reverse=True)
    for r in results:
        print(
            f"  {r['symbol']:>14s}  {r['score']:>8}  {str(r['trades']):>6}  {str(r['win_rate']):>5}"
            f"  {str(r['pnl']):>8}  {str(r['dd']):>6}  {str(r['ema_fast']):>5}  {str(r['ema_slow']):>5}"
            f"  {str(r['sl_tp_pct']):>6}  {str(r['vol_mult']):>5}"
            f"  {str(r['htf_f']):>5}  {str(r['htf_s']):>5}"
        )
    print(f"{'=' * 98}")

    # Save CSV
    import csv
    csv_path = os.path.join(os.path.dirname(__file__), "logs", f"symmetric_opt_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv")
    os.makedirs(os.path.dirname(csv_path), exist_ok=True)
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        if results:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)
    print(f"\n  Results saved -> {csv_path}")


if __name__ == "__main__":
    main()
