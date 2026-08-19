#!/usr/bin/env python3
"""
Walk-forward style WR / RR analysis for the 2xSL & other reward:risk schemes.

Run it yourself (it prints progress as it goes). When it finishes it writes
all results to:
    bot/logs/wr_analysis_results.json

What it does, per pair (downloads 35 days of 5m + 1h data ONCE, then reuses):
  1) Full-close backtest closing at SL or TP=RR*SL for RR in {0.5,1.0,1.5,2.0}
     -> reports trades, win rate, expectancy (in SL units) per RR.
  2) A "tp2x best" summary = the RR 2.0 row (matches live 2xSL scheme).

Usage:
    cd bot
    python wr_analysis.py                     # all config_*.yaml pairs
    python wr_analysis.py --symbols ETHUSDT,BTCUSDT   # only some pairs
    python wr_analysis.py --start 2026-07-09 --end 2026-08-12
"""
import sys, os, asyncio, json, argparse, logging, shutil, subprocess
from pathlib import Path

BOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(BOT))

# ── Interpreter guard: if pandas is missing, re-run with a python that has it ─
try:
    import pandas as pd
except ImportError:
    print("pandas not found in current python. Searching for a python with pandas...", flush=True)
    _candidates = []
    # current real interpreter (even if run through a venv shim)
    _candidates.append(os.path.dirname(sys.executable))
    # common locations
    _candidates.append(r"C:\Users\osdal\AppData\Local\Programs\Python\Python311")
    _candidates.append(r"C:\Users\osdal\AppData\Local\Programs\Python\Python314")
    _candidates.append("")
    _chosen = None
    for base in _candidates:
        exe = os.path.join(base, "python.exe") if base else shutil.which("python")
        if not exe or not os.path.exists(exe):
            continue
        try:
            r = subprocess.run([exe, "-c", "import pandas"], capture_output=True, timeout=30)
            if r.returncode == 0:
                _chosen = exe
                break
        except Exception:
            continue
    if _chosen:
        print(f"Re-running with: {_chosen}", flush=True)
        os.execv(_chosen, [_chosen] + sys.argv)
    else:
        print("ERROR: could not find a python with pandas installed.")
        print('Try:  "C:\\Users\\osdal\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" wr_analysis.py')
        sys.exit(1)

from dotenv import load_dotenv
load_dotenv()

from config import load_config
from strategy import calculate_indicators, calculate_htf_indicators, get_signal, get_htf_trend_latest
from market_data import get_historical_klines
from binance import AsyncClient

# silence noisy loggers
logging.getLogger("bot").setLevel(logging.CRITICAL)
logging.getLogger("strategy").setLevel(logging.CRITICAL)

RRS = [0.5, 1.0, 1.5, 2.0]


def autodetect_symbols() -> list:
    syms = []
    for f in os.listdir(BOT):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            syms.append(sym)
    return sorted(set(syms))


async def download(client, symbol, interval, start, end):
    return await get_historical_klines(client, symbol=symbol, interval=interval, start=start, end=end)


def run_rr(df_raw, df_htf, cfg, rr):
    """Full-close model: open on signal, exit at SL or TP=rr*sl_dist (whichever first)."""
    df = calculate_indicators(df_raw.copy(), cfg)
    if df_htf is not None and cfg.htf_enabled:
        df_htf = calculate_htf_indicators(df_htf.copy(), cfg)
    df = df.dropna()

    entry = None
    trades = wins = sl_hits = tp_hits = 0

    for i in range(1, len(df)):
        bar = df.iloc[i]
        px = float(bar["close"])

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
                    tp_hits += 1
                else:
                    sl_hits += 1
                entry = None
            continue  # while in a position, don't open a new one

        htf_trend = get_htf_trend_latest(df_htf) if (df_htf is not None and cfg.htf_enabled) else None
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
    exp = wr / 100.0 * rr - (1 - wr / 100.0)  # expectancy in SL units
    return {
        "rr": rr, "trades": trades, "wins": wins,
        "win_rate": round(wr, 1), "expectancy_per_sl": round(exp, 4),
    }


async def analyze_pair(client, symbol, cfg, start, end):
    df_5m = await download(client, symbol, "5m", start, end)
    df_1h = None
    if cfg.htf_enabled:
        df_1h = await download(client, symbol, "1h", start, end)
    per_rr = [run_rr(df_5m, df_1h, cfg, rr) for rr in RRS]
    row = {"symbol": symbol, "candles_5m": len(df_5m), "per_rr": per_rr}
    return row


async def main(args):
    symbols = args.symbols or autodetect_symbols()
    start, end = args.start, args.end
    print(f"Period: {start} -> {end} | pairs: {len(symbols)}", flush=True)
    print(f"RR values: {RRS}", flush=True)

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
            cfg.backtest_start = start
            cfg.backtest_end = end
            try:
                row = await analyze_pair(client, sym, cfg, start, end)
                tp2 = {r["rr"] for r in row["per_rr"]}
                s = next(r for r in row["per_rr"] if r["rr"] == 2.0)
                print(
                    f"[{k}/{len(symbols)}] {sym}: RR2x trades={s['trades']:3d} "
                    f"WR={s['win_rate']:5.1f}% | RR1x WR={next((r['win_rate'] for r in row['per_rr'] if r['rr']==1.0),0):5.1f}%",
                    flush=True,
                )
                results.append(row)
            except Exception as e:
                print(f"[{k}/{len(symbols)}] {sym}: ERROR {e}", flush=True)
    finally:
        await client.close_connection()

    out = {
        "period": f"{start}..{end}",
        "strategy_params": {
            "note": "EMA cross + volume + HTF + ADX; full-close at SL or TP=RR*SL",
            "adx_threshold": "from each config_*.yaml",
        },
        "rr_values": RRS,
        "pairs": results,
        "generated_at": pd.Timestamp.now("UTC").isoformat(),
    }
    out_path = BOT / "logs" / "wr_analysis_results.json"
    os.makedirs(out_path.parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False)
    print(f"\nDONE. Results -> {out_path}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None, help="comma-separated, e.g. ETHUSDT,BTCUSDT")
    ap.add_argument("--start", default="2026-07-09")
    ap.add_argument("--end", default="2026-08-12")
    args = ap.parse_args()
    if args.symbols:
        args.symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    asyncio.run(main(args))
