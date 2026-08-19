#!/usr/bin/env python3
"""
Walk-forward with in-window parameter re-fit (Optuna) + out-of-window test.

For a reliable "honest edge" estimate, each pair is evaluated with a true
walk-forward procedure:
    - the whole date range is split into N sequential blocks;
    - fold i trains (Optuna picks params) on block i, then TESTS those params
      out-of-sample on block i+1;
    - only out-of-sample test windows are used to decide tradability.

TP is always forced to 2 x SL (tp1_close_pct=100, single full close), matching
the live scheme and optimizer_tp2x.

Run it yourself. Results -> bot/logs/wf_refit_results.json

Usage:
    cd bot
    python wf_refit.py                          # all config_*.yaml pairs, 4 blocks, 30 trials/fold
    python wf_refit.py --symbols ETHUSDT,SOLUSDT --blocks 4 --trials 30
    python wf_refit.py --start 2026-05-01 --end 2026-08-12 --blocks 4

This is a heavy job: pairs x (blocks-1) folds x trials backtests. Default
(24 pairs, 3 folds, 30 trials) may take 1-3 hours. Reduce --trials / --symbols
for a quick run.
"""
import sys, os, asyncio, json, argparse, logging, shutil, subprocess, time
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path

BOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(BOT))

# ── Interpreter guard ────────────────────────────────────────────────────────
try:
    import pandas as pd
except ImportError:
    print("pandas not found in current python. Searching for a python with pandas...", flush=True)
    _candidates = [os.path.dirname(sys.executable),
                   r"C:\Users\osdal\AppData\Local\Programs\Python\Python311",
                   r"C:\Users\osdal\AppData\Local\Programs\Python\Python314", ""]
    _chosen = None
    for _base in _candidates:
        _exe = os.path.join(_base, "python.exe") if _base else shutil.which("python")
        if not _exe or not os.path.exists(_exe):
            continue
        try:
            if subprocess.run([_exe, "-c", "import pandas"], capture_output=True, timeout=30).returncode == 0:
                _chosen = _exe
                break
        except Exception:
            continue
    if _chosen:
        print(f"Re-running with: {_chosen}", flush=True)
        os.execv(_chosen, [_chosen] + sys.argv)
    else:
        print("ERROR: no python with pandas found. Use Python311 explicitly.")
        sys.exit(1)

from dotenv import load_dotenv
load_dotenv()

import optuna
from binance import AsyncClient

from backtester import BacktestStats, run_backtest_on_df
from config import Config, load_config
from market_data import get_historical_klines
from strategy import calculate_htf_indicators

optuna.logging.set_verbosity(optuna.logging.WARNING)
logging.getLogger("bot").setLevel(logging.CRITICAL)
MIN_TRADES = 5


def autodetect_symbols() -> list:
    syms = []
    for f in os.listdir(BOT):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            syms.append(sym)
    return sorted(set(syms))


# ── scoring + param search (mirrors optimizer_tp2x semantics) ──────────────
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


def build_params(trial: optuna.Trial) -> dict:
    p = {}
    p["ema_fast"] = trial.suggest_int("ema_fast", 5, 20)
    p["ema_slow"] = trial.suggest_int("ema_slow", p["ema_fast"] + 3, 55)
    p["sl_pct"] = trial.suggest_float("sl_pct", 0.2, 1.5, step=0.05)
    p["tp1_pct"] = min(p["sl_pct"] * 2, 3.0)
    p["tp2_pct"] = p["tp1_pct"]
    p["tp1_close_pct"] = 100
    p["volume_multiplier"] = trial.suggest_float("volume_multiplier", 1.0, 2.5, step=0.1)
    p["htf_ema_fast"] = trial.suggest_int("htf_ema_fast", 5, 15)
    p["htf_ema_slow"] = trial.suggest_int("htf_ema_slow", p["htf_ema_fast"] + 3, 40)
    return p


def apply_params(cfg: Config, params: dict) -> Config:
    c = deepcopy(cfg)
    for k, v in params.items():
        setattr(c, k, v)
    c.htf_enabled = True
    c.mode = "backtest"
    return c


def build_objective(base_cfg, df_5m_train, df_htf_full):
    """Optuna objective on the TRAIN window. df_htf_full carries computed HTF
    indicators (causal: get_htf_trend masks by timestamp)."""
    silent_log = logging.getLogger("optuna_trial")
    silent_log.setLevel(logging.CRITICAL)

    def objective(trial):
        params = build_params(trial)
        cfg = apply_params(base_cfg, params)
        # precompute HTF indicators for this trial's HTF params on the FULL htf df
        pre = calculate_htf_indicators(df_htf_full.copy(), cfg)
        stats = asyncio.run(run_backtest_on_df(df_5m_train.copy(), cfg, silent_log, df_htf=pre))
        trial.set_user_attr("total_trades", stats.total_trades)
        trial.set_user_attr("win_rate", round(stats.win_rate, 1))
        trial.set_user_attr("total_pnl", round(stats.total_pnl, 2))
        return score(stats)

    return objective


def refit_train(df_5m_train, df_htf_full, base_cfg, trials, jobs):
    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=42),
    )
    study.optimize(build_objective(base_cfg, df_5m_train, df_htf_full),
                   n_trials=trials, n_jobs=jobs, show_progress_bar=False)
    return study.best_params


def out_of_sample_test(df_5m_test, df_htf_full, base_cfg, params):
    cfg = apply_params(base_cfg, params)
    pre = calculate_htf_indicators(df_htf_full.copy(), cfg)
    silent_log = logging.getLogger("optuna_trial")
    silent_log.setLevel(logging.CRITICAL)
    stats = asyncio.run(run_backtest_on_df(df_5m_test.copy(), cfg, silent_log, df_htf=pre))
    exp = (stats.win_rate / 100.0) * 2.0 - (1 - stats.win_rate / 100.0)  # RR 2:1 expectancy in SL units
    return {
        "trades": stats.total_trades, "win_rate": round(stats.win_rate, 1),
        "pnl": round(stats.total_pnl, 2), "max_drawdown": round(stats.max_drawdown, 2),
        "expectancy_per_sl": round(exp, 4),
    }


async def download(client, symbol, interval, start, end):
    return await get_historical_klines(client, symbol=symbol, interval=interval, start=start, end=end)


def make_blocks(start_str, end_str, n):
    s = datetime.strptime(start_str, "%Y-%m-%d")
    e = datetime.strptime(end_str, "%Y-%m-%d")
    total = (e - s).days
    step = total // n
    blocks = []
    for i in range(n):
        bstart = s + timedelta(days=i * step)
        bend = s + timedelta(days=(i + 1) * step)
        if i == n - 1:
            bend = e
        if bend <= bstart:
            continue
        blocks.append((bstart.strftime("%Y-%m-%d"), bend.strftime("%Y-%m-%d")))
    return blocks


def slice_df(df, start_str, end_str):
    m = (df.index >= str(pd.Timestamp(start_str))) & (df.index <= str(pd.Timestamp(end_str) + pd.Timedelta(days=1)))
    return df.loc[m]


def analyze_pair_sync(df5_full, df1h_full, cfg, n_blocks, trials, jobs, tag):
    """Sync walk-forward (Optuna + backtests). Runs in a worker thread so the
    async backtest can use asyncio.run (no event loop on that thread)."""
    if df1h_full is not None:
        df_htf_full = calculate_htf_indicators(df1h_full.copy(), cfg)
    else:
        df_htf_full = None

    start = cfg.backtest_start
    end = cfg.backtest_end
    blocks = make_blocks(start, end, n_blocks)
    folds = [(blocks[i], blocks[i + 1]) for i in range(len(blocks) - 1)]
    out = {"symbol": cfg.symbol, "candles_5m": len(df5_full), "blocks": blocks,
           "folds": [], "profitable_folds": 0, "avg_expectancy": 0.0, "total_test_trades": 0}

    base_cfg = deepcopy(cfg)
    base_cfg.mode = "backtest"

    for idx, (train_b, test_b) in enumerate(folds, 1):
        df_train = slice_df(df5_full, train_b[0], train_b[1])
        df_test = slice_df(df5_full, test_b[0], test_b[1])
        print(f"  {tag}: fold {idx}/{len(folds)} train {train_b[0]}..{train_b[1]} (Optuna {trials}x jobs={jobs})", flush=True)
        params = refit_train(df_train, df_htf_full, base_cfg, trials, jobs)
        test = out_of_sample_test(df_test, df_htf_full, base_cfg, params)
        print(f"  {tag}: fold {idx}/{len(folds)} test {test_b[0]}..{test_b[1]} -> trades={test['trades']} WR={test['win_rate']}% exp={test['expectancy_per_sl']:+.3f}", flush=True)
        out["folds"].append({
            "fold": idx, "train": train_b, "test": test_b,
            "best_params": {k: (round(v, 4) if isinstance(v, float) else v) for k, v in params.items()},
            "test": test,
        })
        out["total_test_trades"] += test["trades"]
        if test["trades"] >= MIN_TRADES and test["expectancy_per_sl"] > 0:
            out["profitable_folds"] += 1
    if out["folds"]:
        out["avg_expectancy"] = round(sum(f["test"]["expectancy_per_sl"] for f in out["folds"]) / len(out["folds"]), 4)
    return out


async def main(args):
    symbols = args.symbols or autodetect_symbols()
    blocks = make_blocks(args.start, args.end, args.blocks)
    folds_count = len(blocks) - 1
    print(f"Range: {args.start}..{args.end} | blocks={len(blocks)} -> folds={folds_count} | trials/fold={args.trials} | jobs={args.jobs} | pairs={len(symbols)}", flush=True)
    print("Scheme: train params by Optuna on block i, test out-of-sample on block i+1 (TP=2xSL, 100% close)", flush=True)

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)

    results = []
    t0 = time.time()
    try:
        for k, sym in enumerate(symbols, 1):
            symfile = f"config_{sym.replace('USDT', '').lower()}.yaml"
            cfg_path = BOT / symfile
            if not cfg_path.exists():
                print(f"[{k}/{len(symbols)}] {sym}: no config, skipped", flush=True)
                continue
            cfg = load_config(str(cfg_path))
            cfg.backtest_start = args.start
            cfg.backtest_end = args.end
            cfg.mode = "backtest"
            try:
                tag = f"[{k}/{len(symbols)}] {sym}"
                # download full range once (async)
                print(f"{tag}: downloading {cfg.timeframe}+{cfg.htf_timeframe}...", flush=True)
                df5_full = await download(client, sym, cfg.timeframe, args.start, args.end)
                df1h_full = None
                if cfg.htf_enabled:
                    df1h_full = await download(client, sym, cfg.htf_timeframe, args.start, args.end)
                print(f"{tag}: got {len(df5_full)} candles, starting walk-forward (jobs={args.jobs})", flush=True)
                # heavy walk-forward in a worker thread (Optuna uses asyncio.run internally)
                row = await asyncio.to_thread(analyze_pair_sync, df5_full, df1h_full, cfg, args.blocks, args.trials, args.jobs, tag)
                results.append(row)
                pf = row["profitable_folds"]
                print(f"{tag}: DONE profitable {pf}/{folds_count} | avg_exp={row['avg_expectancy']:+.3f} | test_trades={row['total_test_trades']} | {time.time()-t0:.0f}s", flush=True)
            except Exception as e:
                print(f"[{k}/{len(symbols)}] {sym}: ERROR {e}", flush=True)
    finally:
        await client.close_connection()

    out = {
        "range": f"{args.start}..{args.end}",
        "blocks": blocks,
        "folds_count": folds_count,
        "trials_per_fold": args.trials,
        "scheme": "Optuna re-fit on train block, out-of-sample test on next block; TP=2xSL, 100% close",
        "recommend_rule": "profitable (exp>0, >=5 trades) in ALL test folds",
        "pairs": results,
        "generated_at": pd.Timestamp.now("UTC").isoformat(),
    }
    out_path = BOT / "logs" / "wf_refit_results.json"
    os.makedirs(out_path.parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False, default=str)
    print(f"\nDONE ({time.time()-t0:.0f}s). Results -> {out_path}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None, help="comma-separated")
    ap.add_argument("--start", default="2026-05-01")
    ap.add_argument("--end", default="2026-08-12")
    ap.add_argument("--blocks", type=int, default=4)
    ap.add_argument("--trials", type=int, default=30)
    ap.add_argument("--jobs", type=int, default=4, help="Optuna parallel workers (default 4)")
    args = ap.parse_args()
    if args.symbols:
        args.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    asyncio.run(main(args))
