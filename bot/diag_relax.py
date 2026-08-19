#!/usr/bin/env python3
"""
Measure the effect of relaxing volume_multiplier / adx_threshold on win rate,
expectancy and trade frequency (TP=2xSL, 100% close scheme) using real recent
data, so we don't trade frequency for a losing edge.

Per pair (downloads 5m+1h ONCE, then reuses):
  - baseline (current cfg)
  - relaxed: volmult=0.8, adx=15   (and htf on)
  - relaxed: volmult=0.6, adx=10   (and htf on)
  - no volume/adx gate for comparison: volmult VERY low (0.2) + adx=0 (optional)
Reports trades, win rate, expectancy, PnL per setting; aggregates across pairs.

Results -> bot/logs/diag_relax_results.json

Usage:
    cd bot
    python diag_relax.py                      # all 16 active? no: all config_*.yaml
    python diag_relax.py --symbols KASUSDT,ETHUSDT
    python diag_relax.py --start 2026-07-01 --end 2026-08-12
"""
import sys, os, asyncio, json, argparse, logging, shutil, subprocess, time
from copy import deepcopy
from pathlib import Path

BOT = Path(__file__).parent.resolve()
sys.path.insert(0, str(BOT))

# interpreter guard
try:
    import pandas as pd
except ImportError:
    print("pandas not found. searching python with pandas...", flush=True)
    _found = None
    for _base in [os.path.dirname(sys.executable),
                  r"C:\Users\osdal\AppData\Local\Programs\Python\Python311",
                  r"C:\Users\osdal\AppData\Local\Programs\Python\Python314",
                  ""]:
        _exe = os.path.join(_base, "python.exe") if _base else shutil.which("python")
        if not _exe or not os.path.exists(_exe):
            continue
        try:
            if subprocess.run([_exe, "-c", "import pandas"], capture_output=True, timeout=30).returncode == 0:
                _found = _exe
                break
        except Exception:
            continue
    if _found:
        print(f"re-running with {_found}", flush=True)
        os.execv(_found, [_found] + sys.argv)
    print('ERROR: no python with pandas found. Try explicit:')
    print('  "C:\\Users\\osdal\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" diag_relax.py')
    sys.exit(1)

from dotenv import load_dotenv
load_dotenv()
from binance import AsyncClient
from backtester import run_backtest_on_df
from config import load_config
from market_data import get_historical_klines
from strategy import calculate_htf_indicators

logging.getLogger("bot").setLevel(logging.CRITICAL)

# settings under test
SETTINGS = [
    ("base",        None,                  None),
    ("volmult0.8",  0.8,                   15.0),
    ("volmult0.6",  0.6,                   10.0),
    ("nolimit",     0.2,                   0.0),
]


def autodetect_symbols():
    syms = []
    for f in os.listdir(BOT):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            sym = f.replace("config_", "").replace(".yaml", "").upper() + "USDT"
            syms.append(sym)
    return sorted(set(syms))


async def download(client, sym, interval, start, end):
    return await get_historical_klines(client, symbol=sym, interval=interval, start=start, end=end)


def run_set(base_cfg, df5, df_htf, name, vm, adx):
    c = deepcopy(base_cfg)
    c.mode = "backtest"
    if vm is not None:
        c.volume_multiplier = vm
    if adx is not None:
        c.adx_threshold = adx
    pre = calculate_htf_indicators(df_htf.copy(), c) if c.htf_enabled and df_htf is not None else None
    lg = logging.getLogger("trial")
    lg.setLevel(logging.CRITICAL)
    st = asyncio.run(run_backtest_on_df(df5.copy(), c, lg, df_htf=pre))
    # RR 2:1 expectancy in SL units (TP=2xSL, full close)
    exp = (st.win_rate/100.0)*2.0 - (1 - st.win_rate/100.0)
    return {"setting": name, "trades": st.total_trades, "win_rate": round(st.win_rate,1),
            "pnl": round(st.total_pnl,2), "max_drawdown": round(st.max_drawdown,2),
            "expectancy_per_sl": round(exp,4)}


def analyze_sync(df5, df_htf, base_cfg):
    return [run_set(base_cfg, df5, df_htf, name, vm, adx) for name, vm, adx in SETTINGS]


async def main(args):
    symbols = args.symbols or autodetect_symbols()
    print(f"Range: {args.start}..{args.end} | pairs={len(symbols)} | settings={[s[0] for s in SETTINGS]}", flush=True)

    api_key = os.getenv("BINANCE_API_KEY", ""); api_sec = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_sec or None)
    results = []
    t0 = time.time()
    try:
        for k, sym in enumerate(symbols, 1):
            symfile = f"config_{sym.replace('USDT','').lower()}.yaml"
            cpath = BOT / symfile
            if not cpath.exists():
                print(f"[{k}/{len(symbols)}] {sym}: no config, skip", flush=True); continue
            cfg = load_config(str(cpath))
            cfg.backtest_start = args.start; cfg.backtest_end = args.end; cfg.mode = "backtest"
            tag = f"[{k}/{len(symbols)}] {sym}"
            try:
                print(f"{tag}: downloading...", flush=True)
                df5 = await download(client, sym, cfg.timeframe, args.start, args.end)
                df_htf = await download(client, sym, cfg.htf_timeframe, args.start, args.end) if cfg.htf_enabled else None
                res = await asyncio.to_thread(analyze_sync, df5, df_htf, cfg)
                results.append({"symbol": sym, "candles_5m": len(df5), "settings": res})
                base = res[0]
                r08 = next((r for r in res if r["setting"]=="volmult0.8"), base)
                print(f"{tag}: base n={base['trades']} WR={base['win_rate']}% exp={base['expectancy_per_sl']:+.3f} | vol0.8 n={r08['trades']} WR={r08['win_rate']}% exp={r08['expectancy_per_sl']:+.3f} | {time.time()-t0:.0f}s", flush=True)
            except Exception as e:
                print(f"{tag}: ERROR {e}", flush=True)
    finally:
        await client.close_connection()

    out = {"range": f"{args.start}..{args.end}", "settings": [s[0] for s in SETTINGS],
           "scheme": "TP=2xSL, tp1_close_pct=100; report uses current cfg base",
           "pairs": results, "generated_at": pd.Timestamp.now("UTC").isoformat()}
    out_path = BOT / "logs" / "diag_relax_results.json"
    os.makedirs(out_path.parent, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False, default=str)
    print(f"\nDONE ({time.time()-t0:.0f}s). Results -> {out_path}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--start", default="2026-05-01")
    ap.add_argument("--end", default="2026-08-12")
    a = ap.parse_args()
    if a.symbols:
        a.symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    asyncio.run(main(a))
