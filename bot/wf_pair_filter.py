#!/usr/bin/env python3
"""
Walk-forward pair filter for the RR schemes.

Reliably selects which pairs are worth trading by running the signal on
2-3 rolling periods and keeping only pairs that are profitable in most of them.

Run it yourself. Results -> bot/logs/wf_pair_filter_results.json

Per pair (downloads the full date range ONCE, then slices):
  - 3 rolling windows across the history
  - For each window and each RR in {1.0, 1.5, 2.0}: a full-close backtest
    (entry on signal, exit at SL or TP=RR*SL, whichever first).
  - Aggregates: how many windows were profitable, avg expectancy, total edge.

Usage:
    cd bot
    python wf_pair_filter.py                 # all config_*.yaml pairs, 60 days, 3 windows
    python wf_pair_filter.py --symbols ETHUSDT,SOLUSDT
    python wf_pair_filter.py --start 2026-05-01 --end 2026-08-12 --windows 3 --rrs 1.0,1.5,2.0
"""
import sys, os, asyncio, json, argparse, logging, shutil, subprocess
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

from config import load_config
from strategy import calculate_indicators, calculate_htf_indicators, get_signal, get_htf_trend_latest
from market_data import get_historical_klines
from binance import AsyncClient

logging.getLogger("bot").setLevel(logging.CRITICAL)
logging.getLogger("strategy").setLevel(logging.CRITICAL)


def autodetect_symbols() -> list:
    syms = []
    for f in os.listdir(BOT):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            syms.append(sym)
    return sorted(set(syms))


async def download(client, symbol, interval, start, end):
    return await get_historical_klines(client, symbol=symbol, interval=interval, start=start, end=end)


def run_rr_window(df_slice, cfg, rr, df_htf_slice):
    """Full-close model within one window. Recomputes indicators on the slice."""
    df = calculate_indicators(df_slice.copy(), cfg)
    if df_htf_slice is not None and cfg.htf_enabled:
        df_htf = calculate_htf_indicators(df_htf_slice.copy(), cfg)
    else:
        df_htf = None
    df = df.dropna()

    entry = None
    trades = wins = 0
    for i in range(1, len(df)):
        px = float(df.iloc[i]["close"])
        if entry is not None:
            d, eprice, sl, tp = entry
            hit = None
            if d == "LONG":
                if px <= sl: hit = "SL"
                elif px >= tp: hit = "TP"
            else:
                if px >= sl: hit = "SL"
                elif px <= tp: hit = "TP"
            if hit:
                trades += 1
                if hit == "TP":
                    wins += 1
                entry = None
            continue
        htf_trend = get_htf_trend_latest(df_htf) if df_htf is not None else None
        sig = get_signal(df.iloc[: i + 1], cfg, htf_trend=htf_trend)
        if sig is None:
            continue
        sl_dist = sig.entry_price * cfg.sl_pct / 100
        tp_dist = sl_dist * rr
        if sig.direction == "LONG":
            sl, tp = sig.entry_price - sl_dist, sig.entry_price + tp_dist
        else:
            sl, tp = sig.entry_price + sl_dist, sig.entry_price - tp_dist
        entry = (sig.direction, sig.entry_price, sl, tp)

    wr = (wins / trades * 100) if trades else 0.0
    exp = wr / 100.0 * rr - (1 - wr / 100.0)
    return {"trades": trades, "win_rate": round(wr, 1), "expectancy_per_sl": round(exp, 4)}


def make_windows(start_str, end_str, windows):
    """Return list of (start, end) date strings, one per window (aligned, non-overlapping)."""
    start = datetime.strptime(start_str, "%Y-%m-%d")
    end = datetime.strptime(end_str, "%Y-%m-%d")
    total = (end - start).days
    step = total // windows
    out = []
    for i in range(windows):
        wstart = start + timedelta(days=i * step)
        wend = start + timedelta(days=(i + 1) * step)
        if i == windows - 1:
            wend = end
        if wend <= wstart:
            continue
        out.append((wstart.strftime("%Y-%m-%d"), wend.strftime("%Y-%m-%d")))
    return out


async def main(args):
    symbols = args.symbols or autodetect_symbols()
    windows = make_windows(args.start, args.end, args.windows)
    rrs = args.rrs
    print(f"Range: {args.start}..{args.end} | windows={len(windows)} | rrs={rrs} | pairs={len(symbols)}", flush=True)
    for w in windows:
        print(f"  window: {w[0]} -> {w[1]}", flush=True)

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_secret or None)

    results = []
    try:
        for k, sym in enumerate(symbols, 1):
            symfile = f"config_{sym.replace('USDT', '').lower()}.yaml"
            cfg_path = BOT / symfile
            if not cfg_path.exists():
                print(f"[{k}/{len(symbols)}] {sym}: no config, skipped", flush=True)
                continue
            cfg = load_config(str(cfg_path))
            cfg.mode = "backtest"
            try:
                # download full range once
                df5 = await download(client, sym, "5m", args.start, args.end)
                df1h = None
                if cfg.htf_enabled:
                    df1h = await download(client, sym, "1h", args.start, args.end)
                per_rr = {}
                for rr in rrs:
                    per_rr[rr] = {"windows": [], "profitable_windows": 0, "avg_expectancy": 0.0, "total_trades": 0}
                for (ws, we) in windows:
                    # slice 5m by date (index carries the timestamp)
                    mask5 = (df5.index >= str(pd.Timestamp(ws))) & (df5.index <= str(pd.Timestamp(we) + pd.Timedelta(days=1)))
                    s5 = df5.loc[mask5]
                    s1h = None
                    if df1h is not None:
                        mask1 = (df1h.index >= str(pd.Timestamp(ws))) & (df1h.index <= str(pd.Timestamp(we) + pd.Timedelta(days=1)))
                        s1h = df1h.loc[mask1]
                    for rr in rrs:
                        r = run_rr_window(s5, cfg, rr, s1h)
                        pr = per_rr[rr]
                        pr["windows"].append(r)
                        pr["total_trades"] += r["trades"]
                        if r["expectancy_per_sl"] > 0:
                            pr["profitable_windows"] += 1
                for rr in rrs:
                    pr = per_rr[rr]
                    nw = len(pr["windows"])
                    avg = sum(w["expectancy_per_sl"] for w in pr["windows"]) / nw if nw else 0.0
                    pr["avg_expectancy"] = round(avg, 4)
                # recommendation: profitable in >= 2 of 3 windows at RR 1.5
                rec1 = per_rr[1.5]["profitable_windows"] >= 2 if 1.5 in rrs else False
                rec = {"rr15_ok_2of3": rec1, "rr15_profitable_windows": per_rr.get(1.5, {}).get("profitable_windows", 0)}
                results.append({"symbol": sym, "candles_5m": len(df5), "per_rr": per_rr, "recommend": rec})
                line = (f"[{k}/{len(symbols)}] {sym}: "
                        + " | ".join(f"RR{rr} WR={per_rr[rr]['windows'][-1]['win_rate']}% exp={per_rr[rr]['avg_expectancy']:+.3f} prof={per_rr[rr]['profitable_windows']}/{len(windows)}" for rr in rrs))
                print(line, flush=True)
            except Exception as e:
                print(f"[{k}/{len(symbols)}] {sym}: ERROR {e}", flush=True)
    finally:
        await client.close_connection()

    out = {
        "range": f"{args.start}..{args.end}",
        "windows": windows,
        "rr_values": rrs,
        "recommend_rule": "profitable (exp>0) in >=2 of %d windows at RR 1.5" % max(1, len(windows)),
        "pairs": results,
        "generated_at": pd.Timestamp.now("UTC").isoformat(),
    }
    out_path = BOT / "logs" / "wf_pair_filter_results.json"
    os.makedirs(out_path.parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print(f"\nDONE. Results -> {out_path}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None, help="comma-separated")
    ap.add_argument("--start", default="2026-05-01")
    ap.add_argument("--end", default="2026-08-12")
    ap.add_argument("--windows", type=int, default=3)
    ap.add_argument("--rrs", default="1.0,1.5,2.0", help="comma-separated RR values")
    args = ap.parse_args()
    if args.symbols:
        args.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    args.rrs = [float(x) for x in args.rrs.split(",") if x.strip()]
    asyncio.run(main(args))
