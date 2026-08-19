#!/usr/bin/env python3
"""
Walk-forward batch optimizer for replit_scalper.

Usage:
    python walk_forward_opt.py                          # defaults: 200 trials, 2 jobs, 4 parallel
    python walk_forward_opt.py --trials 300 --jobs 4
    python walk_forward_opt.py --start 2026-07-01 --end 2026-08-06
    python walk_forward_opt.py --skip-mc                 # skip Monte Carlo
    python walk_forward_opt.py --batch-size 3            # 3 pairs in parallel
    python walk_forward_opt.py --symbols ETHUSDT,BTCUSDT # only specific pairs

Features:
    - Walk-forward: 2 folds (train/test split)
    - 200+ trials per fold (configurable)
    - Batch execution with console progress
    - Error logging to console
    - Optional Monte Carlo bootstrap
    - Auto-apply best params to configs
"""

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None

SCRIPT_PATH = Path(__file__).resolve()
BOT_DIR = Path(__file__).parent
LOGS_DIR = BOT_DIR / "logs"
LOCK_PATH = BOT_DIR / "walk_forward_opt.lock"

# ── Single instance guard ────────────────────────────────────────────────────

def _ensure_single_instance() -> None:
    """
    Prevent multiple instances of walk_forward_opt.py from running at once.
    Uses a simple PID-based lock file.
    """
    import time

    if LOCK_PATH.exists():
        try:
            old_pid = int(LOCK_PATH.read_text().strip())
            # Check if process still exists; on Windows os.kill can misbehave,
            # so wrap broadly and treat any check failure as "stale".
            try:
                os.kill(old_pid, 0)
            except Exception:
                print(f"[LOCK] Stale lock file found (PID {old_pid} no longer exists). Removing.")
                LOCK_PATH.unlink(missing_ok=True)
            else:
                print(f"[LOCK] Another instance is already running (PID {old_pid}).")
                print(f"       Lock file: {LOCK_PATH}")
                print("       If you're sure it's stale, delete the lock file and retry.")
                sys.exit(1)
        except (ValueError, OSError) as e:
            print(f"[LOCK] Invalid lock file, removing: {e}")
            LOCK_PATH.unlink(missing_ok=True)

    LOCK_PATH.write_text(str(os.getpid()))

    def cleanup_lock():
        try:
            LOCK_PATH.unlink(missing_ok=True)
        except OSError:
            pass

    import atexit
    atexit.register(cleanup_lock)

# ── Configuration ────────────────────────────────────────────────────────────

PAIR_CONFIGS = {
    "ATOMUSDT": "config_atom.yaml",
    "DOGEUSDT": "config_doge.yaml",
    "ETHUSDT": "config_eth.yaml",
    "SOLUSDT": "config_sol.yaml",
    "INJUSDT": "config_inj.yaml",
    "OPUSDT": "config_op.yaml",
    "POLUSDT": "config_pol.yaml",
    "ONTUSDT": "config_ont.yaml",
    "HBARUSDT": "config_hbar.yaml",
    "NEARUSDT": "config_near.yaml",
    "SUIUSDT": "config_sui.yaml",
    "FILUSDT": "config_fil.yaml",
    "KASUSDT": "config_kas.yaml",
    "XRPUSDT": "config_xrp.yaml",
    "LINKUSDT": "config_link.yaml",
    "DOTUSDT": "config_dot.yaml",
    "TRXUSDT": "config_trx.yaml",
    "BTCUSDT": "config_btc.yaml",
    "BNBUSDT": "config_bnb.yaml",
    "AVAXUSDT": "config_avax.yaml",
    "ADAUSDT": "config_ada.yaml",
    "1000PEPEUSDT": "config_1000pepe.yaml",
    "ARBUSDT": "config_arb.yaml",
    "APTUSDT": "config_apt.yaml",
}

BOT_DIR = Path(__file__).parent
LOGS_DIR = BOT_DIR / "logs"


def parse_date(date_str: str) -> datetime:
    return datetime.strptime(date_str, "%Y-%m-%d")


def format_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def generate_walk_forward_folds(start: datetime, end: datetime, n_folds: int = 2):
    """
    Generate train/test date ranges for walk-forward optimization.
    Uses expanding/rolling window approach.
    """
    total_days = (end - start).days
    fold_size = total_days // n_folds
    train_size = int(fold_size * 0.7)  # 70% train, 30% test

    folds = []
    for i in range(n_folds):
        fold_start = start + timedelta(days=i * fold_size)
        fold_end = min(fold_start + timedelta(days=fold_size), end)

        train_start = fold_start
        train_end = fold_start + timedelta(days=train_size)
        test_start = train_end
        test_end = fold_end

        if test_start < test_end:
            folds.append({
                "fold": i + 1,
                "train_start": format_date(train_start),
                "train_end": format_date(train_end),
                "test_start": format_date(test_start),
                "test_end": format_date(test_end),
            })

    return folds


def run_optimizer(pair: str, config: str, start: str, end: str, trials: int, jobs: int, study_name: str) -> dict:
    """
    Run optimizer.py for a single pair/date range.
    Returns dict with status, csv_path, best_params, error.
    """
    import time

    cmd = [
        sys.executable, "optimizer.py",
        "--config", config,
        "--symbol", pair,
        "--start", start,
        "--end", end,
        "--trials", str(trials),
        "--jobs", str(jobs),
    ]

    try:
        result = subprocess.run(
            cmd,
            cwd=str(BOT_DIR),
            capture_output=True,
            text=True,
            timeout=1800,  # 30 minutes per fold
        )

        # Wait for filesystem to flush
        time.sleep(2)

        # Find the latest optimization CSV for this pair
        pattern = str(LOGS_DIR / f"optimization_{pair}_*.csv")
        csv_files = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)

        if not csv_files:
            return {
                "status": "error",
                "error": "No CSV output found",
                "stdout": result.stdout[-500:] if result.stdout else "",
                "stderr": result.stderr[-500:] if result.stderr else "",
            }

        csv_path = csv_files[0]

        # Wait for CSV to have data (in case of async write)
        for attempt in range(30):  # up to 15 seconds
            try:
                with open(csv_path) as f:
                    lines = f.readlines()
                if len(lines) >= 1:
                    break
            except (IOError, OSError):
                pass
            time.sleep(0.5)
        else:
            return {
                "status": "error",
                "error": "CSV not ready after waiting",
                "csv_path": csv_path,
            }

        # Check if there are any profitable trials
        if len(lines) == 1 and "No profitable trials found" in lines[0]:
            return {
                "status": "error",
                "error": "No profitable trials found in this fold",
                "csv_path": csv_path,
            }

        if len(lines) < 2:
            return {
                "status": "error",
                "error": "CSV has no data rows",
                "csv_path": csv_path,
            }

        best = lines[1].strip().split(",")
        headers = lines[0].strip().split(",")

        params = {}
        for i, h in enumerate(headers):
            if i < len(best):
                params[h] = best[i]

        return {
            "status": "ok",
            "csv_path": csv_path,
            "best_params": params,
            "score": params.get("score", "0"),
            "stdout": result.stdout[-200:] if result.stdout else "",
        }

        params = {}
        for i, h in enumerate(headers):
            if i < len(best):
                params[h] = best[i]

        return {
            "status": "ok",
            "csv_path": csv_path,
            "best_params": params,
            "score": params.get("score", "0"),
            "stdout": result.stdout[-200:] if result.stdout else "",
        }

    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Timeout after 10 minutes"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def run_backtest(pair: str, params: dict, start: str, end: str) -> dict:
    """
    Run backtest_runner.py with given params on test period.
    """
    import yaml

    config_file = PAIR_CONFIGS.get(pair, f"config_{pair.lower()}.yaml")
    config_path = BOT_DIR / config_file

    # Load base config
    if config_path.exists():
        with open(config_path) as f:
            base_config = yaml.safe_load(f) or {}
    else:
        base_config = {}

    # Build config overrides with all required fields
    config_overrides = {
        "symbol": pair,
        "timeframe": base_config.get("timeframe", "5m"),
        "leverage": base_config.get("leverage", 150),
        "volume_ma_period": base_config.get("volume_ma_period", 20),
        "auto_mode": base_config.get("auto_mode", True),
        "paper_balance": base_config.get("paper_balance", 1000.0),
        "mode": "backtest",
        "backtest_start": start,
        "backtest_end": end,
        "log_file": "logs/backtest.log",
        "margin_pct": base_config.get("margin_pct", 10),
        "fixed_notional_usd": base_config.get("fixed_notional_usd", 0),
        "fixed_qty": base_config.get("fixed_qty", 0.0),
        "fixed_risk_usd": base_config.get("fixed_risk_usd", 0),
        "htf_enabled": base_config.get("htf_enabled", True),
        "htf_timeframe": base_config.get("htf_timeframe", "1h"),
        "htf_ema_fast": int(params.get("htf_ema_fast", base_config.get("htf_ema_fast", 10))),
        "htf_ema_slow": int(params.get("htf_ema_slow", base_config.get("htf_ema_slow", 31))),
        "ema_fast": int(params.get("ema_fast", base_config.get("ema_fast", 10))),
        "ema_slow": int(params.get("ema_slow", base_config.get("ema_slow", 30))),
        "adx_period": base_config.get("adx_period", 14),
        "adx_threshold": base_config.get("adx_threshold", 20.0),
        "recovery_max_position_pct": base_config.get("recovery_max_position_pct", 100.0),
    }
    # TP = 2x SL single-level scheme, full close at TP1 (must match apply_params_to_config
    # and the live bots). Deriving tp1/tp2 from sl guarantees the backtest is optimised
    # under the SAME RR 2:1 that is actually applied to configs.
    wf_sl = float(params.get("sl_pct", base_config.get("sl_pct", 1.0)))
    wf_tp = round(min(wf_sl * 2, 3.0), 2)
    config_overrides["sl_pct"] = wf_sl
    config_overrides["tp1_pct"] = wf_tp
    config_overrides["tp2_pct"] = wf_tp
    config_overrides["tp1_close_pct"] = 100
    # volume_multiplier / risk_pct still taken from params
    config_overrides["volume_multiplier"] = float(params.get("volume_multiplier", base_config.get("volume_multiplier", 1.5)))
    config_overrides["risk_pct"] = float(params.get("risk_pct", base_config.get("risk_pct", 3.0)))

    payload = {
        "symbol": pair,
        "start": start,
        "end": end,
        "config": {k: v for k, v in config_overrides.items() if k not in ("symbol", "mode", "backtest_start", "backtest_end", "log_file")},
    }

    try:
        result = subprocess.run(
            [sys.executable, "backtest_runner.py"],
            cwd=str(BOT_DIR),
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=300,  # 5 minutes
        )

        if result.returncode != 0:
            return {
                "status": "error",
                "error": f"Backtest failed: {result.stderr[-200:]}",
            }

        # Parse JSON output
        output_lines = result.stdout.strip().split("\n")
        for line in output_lines:
            try:
                data = json.loads(line)
                if "error" in data:
                    return {"status": "error", "error": data["error"]}
                return {"status": "ok", "result": data}
            except json.JSONDecodeError:
                continue

        return {"status": "error", "error": "No JSON in output"}

    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Backtest timeout"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def monte_carlo_bootstrap(trades: list, n_simulations: int = 10000) -> dict:
    """
    Bootstrap Monte Carlo simulation of trade sequence.
    trades: list of (pnl, win_rate) or similar
    Returns distribution stats.
    """
    import random

    if not trades:
        return {"error": "No trades for Monte Carlo"}

    # Simulate 10,000 sequences
    pnl_distribution = []
    for _ in range(n_simulations):
        # Resample trades with replacement
        sampled = random.choices(trades, k=len(trades))
        total_pnl = sum(t.get("pnl", 0) for t in sampled)
        pnl_distribution.append(total_pnl)

    pnl_distribution.sort()

    return {
        "n_simulations": n_simulations,
        "median_pnl": pnl_distribution[n_simulations // 2],
        "p5_pnl": pnl_distribution[int(n_simulations * 0.05)],
        "p95_pnl": pnl_distribution[int(n_simulations * 0.95)],
        "min_pnl": pnl_distribution[0],
        "max_pnl": pnl_distribution[-1],
        "positive_pct": sum(1 for p in pnl_distribution if p > 0) / n_simulations * 100,
    }


def optimize_pair_walk_forward(pair: str, config: str, start: str, end: str, trials: int, jobs: int, skip_mc: bool) -> dict:
    """
    Run walk-forward optimization for a single pair.
    """
    start_dt = parse_date(start)
    end_dt = parse_date(end)
    folds = generate_walk_forward_folds(start_dt, end_dt, n_folds=2)

    fold_results = []
    all_trades = []

    for fold in folds:
        print(f"\n  [{pair}] Fold {fold['fold']}: train {fold['train_start']}..{fold['train_end']}, test {fold['test_start']}..{fold['test_end']}")

        # Step 1: Optimize on train data
        opt_result = run_optimizer(
            pair, config,
            fold["train_start"], fold["train_end"],
            trials // len(folds), jobs,
            f"wf_{pair}_fold{fold['fold']}"
        )

        if opt_result["status"] == "error":
            error_msg = opt_result.get("error", "Unknown")
            if "No profitable trials found" in error_msg:
                print(f"    [WARN] No profitable trials in fold {fold['fold']}, skipping")
                fold_results.append({
                    "fold": fold["fold"],
                    "train_score": 0,
                    "test_pnl": 0,
                    "test_wr": 0,
                    "test_trades": 0,
                    "params": {},
                    "skipped": True,
                })
                continue
            return {
                "pair": pair,
                "status": "error",
                "error": f"Fold {fold['fold']} optimization failed: {error_msg}",
                "stdout": opt_result.get("stdout", ""),
                "stderr": opt_result.get("stderr", ""),
            }

        print(f"    Train best score: {opt_result.get('score', 'N/A')}")

        # Step 2: Test on test data
        test_result = run_backtest(
            pair, opt_result["best_params"],
            fold["test_start"], fold["test_end"]
        )

        if test_result["status"] == "error":
            return {
                "pair": pair,
                "status": "error",
                "error": f"Fold {fold['fold']} backtest failed: {test_result['error']}",
            }

        test_stats = test_result.get("result", {})
        print(f"    Test result: trades={test_stats.get('total_trades', 0)}, wr={test_stats.get('win_rate', 0)}%, pnl={test_stats.get('total_pnl', 0)}")

        fold_results.append({
            "fold": fold["fold"],
            "train_score": opt_result.get("score", 0),
            "test_pnl": test_stats.get("total_pnl", 0),
            "test_wr": test_stats.get("win_rate", 0),
            "test_trades": test_stats.get("total_trades", 0),
            "params": opt_result["best_params"],
            "skipped": False,
        })

        # Collect trades for Monte Carlo (simplified)
        if test_stats.get("total_trades", 0) > 0:
            all_trades.append({
                "pnl": test_stats.get("total_pnl", 0),
                "wr": test_stats.get("win_rate", 0),
            })

    # Filter out skipped folds
    valid_folds = [f for f in fold_results if not f.get("skipped")]
    if not valid_folds:
        return {
            "pair": pair,
            "status": "error",
            "error": "No profitable folds found in walk-forward",
            "folds": fold_results,
        }

    # Step 3: Aggregate and select best params
    # Select params from fold with best test performance
    best_fold = max(valid_folds, key=lambda x: x["test_pnl"])

    result = {
        "pair": pair,
        "status": "ok",
        "folds": fold_results,
        "best_params": best_fold["params"],
        "best_fold": best_fold["fold"],
        "avg_test_pnl": sum(f["test_pnl"] for f in valid_folds) / len(valid_folds),
        "avg_test_wr": sum(f["test_wr"] for f in valid_folds) / len(valid_folds),
    }

    # Step 4: Monte Carlo (optional)
    if not skip_mc and all_trades:
        print(f"  [{pair}] Running Monte Carlo bootstrap...")
        mc_result = monte_carlo_bootstrap(all_trades, n_simulations=5000)
        result["monte_carlo"] = mc_result
        print(f"    MC median PnL: {mc_result.get('median_pnl', 0):.2f}, 5th-95th percentile: {mc_result.get('p5_pnl', 0):.2f} / {mc_result.get('p95_pnl', 0):.2f}")

    return result


def apply_params_to_config(pair: str, params: dict):
    """
    Apply optimized params to the YAML config file.
    """
    config_file = PAIR_CONFIGS.get(pair)
    if not config_file:
        return False

    config_path = BOT_DIR / config_file
    if not config_path.exists():
        return False

    content = config_path.read_text(encoding="utf-8")

    # TP = 2x SL single-level scheme (full close at TP1, no TP1/TP2 split).
    # sl_pct is optimized independently; tp1/tp2 are derived so the take-profit
    # is always exactly twice the stop-loss and closes 100% at once. This keeps
    # the scheme consistent across daily re-optimizations.
    sl_pct = float(params.get('sl_pct', '1.0'))
    tp_pct = round(min(sl_pct * 2, 3.0), 2)  # TP = 2 * SL, cap 3%

    # Update numeric params
    replacements = {
        r"ema_fast: \d+": f"ema_fast: {params.get('ema_fast', '11')}",
        r"ema_slow: \d+": f"ema_slow: {params.get('ema_slow', '15')}",
        r"sl_pct: [\d.]+": f"sl_pct: {sl_pct}",
        r"tp1_pct: [\d.]+": f"tp1_pct: {tp_pct}",
        r"tp2_pct: [\d.]+": f"tp2_pct: {tp_pct}",
        r"volume_multiplier: [\d.]+": f"volume_multiplier: {params.get('volume_multiplier', '1.5')}",
        r"tp1_close_pct: \d+": "tp1_close_pct: 100",
        r"risk_pct: [\d.]+": f"risk_pct: {params.get('risk_pct', '3.0')}",
    }

    for pattern, replacement in replacements.items():
        content = re.sub(pattern, replacement, content)

    config_path.write_text(content, encoding="utf-8")
    return True


def process_pair(pair: str, config: str, start: str, end: str, trials: int, jobs: int, skip_mc: bool) -> dict:
    """
    Worker function for parallel execution.
    """
    try:
        result = optimize_pair_walk_forward(pair, config, start, end, trials, jobs, skip_mc)
        if result.get("status") == "ok":
            apply_params_to_config(pair, result["best_params"])
            result["config_updated"] = True
        return result
    except Exception as e:
        return {"pair": pair, "status": "error", "error": str(e)}


def main():
    _ensure_single_instance()

    parser = argparse.ArgumentParser(
        description="Walk-forward batch optimizer for replit_scalper",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--trials", type=int, default=200, help="Trials per fold (default: 200)")
    parser.add_argument("--jobs", type=int, default=2, help="Parallel jobs per optimizer (default: 2)")
    parser.add_argument("--batch-size", type=int, default=4, help="Pairs in parallel (default: 4)")
    parser.add_argument("--start", default="2026-07-01", help="Start date YYYY-MM-DD")
    parser.add_argument("--end", default="2026-08-06", help="End date YYYY-MM-DD")
    parser.add_argument("--folds", type=int, default=2, help="Number of walk-forward folds (default: 2)")
    parser.add_argument("--skip-mc", action="store_true", help="Skip Monte Carlo bootstrap")
    parser.add_argument("--symbols", default=None, help="Comma-separated list of symbols")
    parser.add_argument("--no-apply", action="store_true", help="Don't apply params to configs")
    args = parser.parse_args()

    # Determine which pairs to optimize
    if args.symbols:
        pairs = [s.strip() for s in args.symbols.split(",")]
        pair_configs = {p: PAIR_CONFIGS[p] for p in pairs if p in PAIR_CONFIGS}
    else:
        pair_configs = PAIR_CONFIGS.copy()

    if not pair_configs:
        print("No valid pairs to optimize.")
        sys.exit(1)

    print("=" * 70)
    print("  Walk-Forward Batch Optimizer")
    print("=" * 70)
    print(f"  Pairs:      {len(pair_configs)}")
    print(f"  Period:     {args.start} -> {args.end}")
    print(f"  Trials:     {args.trials} per fold")
    print(f"  Jobs:       {args.jobs} per optimizer")
    print(f"  Folds:      {args.folds}")
    print(f"  Batch size: {args.batch_size} parallel")
    print(f"  Monte Carlo: {'SKIP' if args.skip_mc else 'YES'}")
    print("=" * 70)
    print()

    # Run optimization in batches
    pairs_list = list(pair_configs.items())
    total_batches = (len(pairs_list) + args.batch_size - 1) // args.batch_size

    all_results = []

    for batch_idx in range(total_batches):
        batch_start = batch_idx * args.batch_size
        batch_end = min(batch_start + args.batch_size, len(pairs_list))
        batch = pairs_list[batch_start:batch_end]

        print(f"\n{'=' * 70}")
        print(f"  Batch {batch_idx + 1}/{total_batches}: {', '.join(p for p, _ in batch)}")
        print(f"{'=' * 70}")

        with ProcessPoolExecutor(max_workers=args.batch_size) as executor:
            futures = {
                executor.submit(
                    process_pair,
                    pair, config, args.start, args.end,
                    args.trials, args.jobs, args.skip_mc
                ): pair
                for pair, config in batch
            }

            for future in as_completed(futures):
                pair = futures[future]
                try:
                    result = future.result()
                    all_results.append(result)

                    if result.get("status") == "ok":
                        avg_pnl = result.get("avg_test_pnl", 0)
                        avg_wr = result.get("avg_test_wr", 0)
                        print(f"\n  [OK] {pair}: avg_test_pnl={avg_pnl:.2f}, avg_wr={avg_wr:.1f}%")
                        if result.get("monte_carlo"):
                            mc = result["monte_carlo"]
                            print(f"    Monte Carlo: median={mc.get('median_pnl', 0):.2f}, 5th-95th: {mc.get('p5_pnl', 0):.2f} / {mc.get('p95_pnl', 0):.2f}")
                        if result.get("config_updated"):
                            print(f"    Config updated with best params from fold {result.get('best_fold')}")
                    else:
                        print(f"\n  [ERR] {pair}: ERROR - {result.get('error', 'Unknown')}")
                        if result.get("stdout"):
                            print(f"    stdout: {result['stdout'][:200]}")
                        if result.get("stderr"):
                            print(f"    stderr: {result['stderr'][:200]}")

                except Exception as e:
                    print(f"\n  [ERR] {pair}: EXCEPTION - {e}")
                    all_results.append({"pair": pair, "status": "error", "error": str(e)})

    # Final summary
    print(f"\n{'=' * 70}")
    print("  FINAL RESULTS")
    print(f"{'=' * 70}")

    ok_count = sum(1 for r in all_results if r.get("status") == "ok")
    fail_count = len(all_results) - ok_count

    print(f"  Success: {ok_count}/{len(all_results)}")
    if fail_count > 0:
        print(f"  Failed:  {fail_count}")
        for r in all_results:
            if r.get("status") != "ok":
                print(f"    - {r.get('pair')}: {r.get('error', 'Unknown')}")

    print()
    for r in all_results:
        if r.get("status") == "ok":
            print(f"  {r['pair']:15s} | avg_pnl={r.get('avg_test_pnl', 0):8.2f} | avg_wr={r.get('avg_test_wr', 0):5.1f}%")

    print(f"\n{'=' * 70}")
    print("  Optimization complete!")
    print(f"{'=' * 70}")


if __name__ == "__main__":
    main()
