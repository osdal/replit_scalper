#!/usr/bin/env python3
"""
Scalper-level analysis: compare SL/TP distances on a proper backtest.

Motivation: current SL/TP (0.6-1.5% / 1.2-3%) are far wider than real 5m moves,
so positions "hang" for hours (median ~3-4h, many open >8h). This script derives
scalper-appropriate levels from the pair's real 5m ATR and measures the effect
on win rate, trade frequency, expectancy and hold time vs the current levels.

Level variants (TP = 2 x SL in every variant):
  wide      : current cfg.sl_pct / cfg.tp1_pct          (baseline)
  scalper1  : SL = 1.0 x ATR(14), TP = 2 x SL           (in % of price)
  scalper2  : SL = 0.7 x ATR(14), TP = 2 x SL           (tighter / faster)
  mid       : SL = 0.5 x wide_sl , TP = 2 x SL

Signal is FIXED per pair (current ema/vol/adx/htf params), so the comparison
isolates the effect of SL/TP tightness on speed & edge.

Per variant reports: trades, win_rate, expectancy-per-SL-unit, avg hold
(5m candles), and % closed within 30/60/120 min.

Results -> bot/logs/diag_scalper_results.json

Usage:
    cd bot
    python diag_scalper.py                      # all config_*.yaml pairs
    python diag_scalper.py --symbols KASUSDT,ETHUSDT
    python diag_scalper.py --start 2026-07-01 --end 2026-08-12

RUNTIME: ~2-4 min per pair (16 pairs ~30-60 min). Reduce --start/--end (fewer
days) or --symbols to shorten.
"""
import sys, os, asyncio, json, argparse, logging, shutil, subprocess, time
from copy import deepcopy
from pathlib import Path

BOT = Path(__file__).parent.resolve(); sys.path.insert(0, str(BOT))

try:
    import pandas as pd
    import numpy as np
except ImportError:
    print("pandas/numpy not found in this python. searching a python with both...", flush=True)
    _found = None
    for _base in [os.path.dirname(sys.executable),
                  r"C:\Users\osdal\AppData\Local\Programs\Python\Python311",
                  r"C:\Users\osdal\AppData\Local\Programs\Python\Python314",
                  ""]:
        _exe = os.path.join(_base, "python.exe") if _base else shutil.which("python")
        if not _exe or not os.path.exists(_exe): continue
        try:
            r = subprocess.run([_exe, "-c", "import pandas, numpy"], capture_output=True, timeout=30)
            if r.returncode == 0:
                _found = _exe; break
        except Exception: pass
    if _found:
        print(f"re-running with {_found}", flush=True)
        os.execv(_found, [_found] + sys.argv)
    else:
        print('ERROR: no python with both pandas and numpy. Use:')
        print('  "C:\\Users\\osdal\\AppData\\Local\\Programs\\Python\\Python311\\python.exe" diag_scalper.py')
        sys.exit(1)

from dotenv import load_dotenv
load_dotenv()
from binance import AsyncClient
from config import load_config
from market_data import get_historical_klines
from strategy import calculate_indicators, calculate_htf_indicators, get_signal, get_htf_trend_latest

logging.getLogger("bot").setLevel(logging.CRITICAL)


def autodetect_symbols():
    syms = []
    for f in os.listdir(BOT):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            syms.append(f.replace("config_", "").replace(".yaml", "").upper() + "USDT")
    return sorted(set(syms))


async def download(client, sym, interval, start, end):
    return await get_historical_klines(client, symbol=sym, interval=interval, start=start, end=end)


def make_levels(cfg):
    """Return list of (name, sl_frac) where sl_frac = SL distance as fraction of price.
    TP is always 2 x SL."""
    wide_sl = cfg.sl_pct / 100.0
    levels = [("wide", wide_sl)]
    # scalper from ATR: need a representative ATR value; we'll compute per-entry in loop.
    # We mark these so the loop derives per-entry from the atr column.
    levels.append(("scalper1", "atr1.0"))
    levels.append(("scalper2", "atr0.7"))
    mid_sl = wide_sl * 0.5
    levels.append(("mid", mid_sl))
    return levels


def run_level(df, df_htf, cfg, sl_spec):
    """Full-close backtest closing at SL or TP=2*SL. Returns stats dict.
    sl_spec: float (fraction) or 'atrK' meaning derive per-entry = K*ATR(% of price)."""
    df = df.copy()
    if df_htf is not None and cfg.htf_enabled:
        df_htf = df_htf.copy()
    else:
        df_htf = None
    entry = None
    trades = wins = 0
    hold_list = []
    close_30 = close_60 = close_120 = 0
    # arrays
    closes = df["close"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    atr = df["atr"].to_numpy(float) if "atr" in df.columns else None

    def dist(i):
        if isinstance(sl_spec, str) and sl_spec.startswith("atr"):
            k = float(sl_spec.replace("atr", ""))
            base = atr[i] / closes[i] if atr is not None and atr[i] > 0 else cfg.sl_pct / 100.0
            return max(base * k, cfg.sl_pct / 100.0 * 0.05)  # floor tiny sl
        return sl_spec

    for i in range(1, len(df)):
        if entry is not None:
            d, slv, tpv, start_i = entry
            if d == "LONG":
                hit = "SL" if lows[i] <= slv else ("TP" if highs[i] >= tpv else None)
            else:
                hit = "TP" if lows[i] <= tpv else ("SL" if highs[i] >= slv else None)
            if hit:
                hi = i - start_i
                hold_list.append(hi)
                trades += 1
                if hit == "TP": wins += 1
                if hi <= 6: close_30 += 1
                if hi <= 12: close_60 += 1
                if hi <= 24: close_120 += 1
                entry = None
            continue
        htf_trend = get_htf_trend_latest(df_htf) if df_htf is not None else None
        sig = get_signal(df.iloc[: i + 1], cfg, htf_trend=htf_trend)
        if sig is None: continue
        sl_frac = dist(i)
        eprice = sig.entry_price
        sl_dist = eprice * sl_frac
        tp_dist = sl_dist * 2.0
        if sig.direction == "LONG": slv, tpv = eprice - sl_dist, eprice + tp_dist
        else: slv, tpv = eprice + sl_dist, eprice - tp_dist
        entry = (sig.direction, slv, tpv, i)

    hold = np.array(hold_list) if hold_list else np.array([0])
    wr = (wins / trades * 100) if trades else 0.0
    exp = (wr / 100.0) * 2.0 - (1 - wr / 100.0)   # RR 2:1, per SL unit
    return {
        "trades": trades, "win_rate": round(wr, 1),
        "expectancy_per_sl": round(exp, 4),
        "avg_hold_candles": round(float(hold.mean()), 1),
        "avg_hold_min": round(float(hold.mean()) * 5, 1),
        "pct_closed_30min": round(100 * close_30 / trades, 1) if trades else 0,
        "pct_closed_60min": round(100 * close_60 / trades, 1) if trades else 0,
        "pct_closed_120min": round(100 * close_120 / trades, 1) if trades else 0,
    }


def analyze_sync(df5, df_htf, cfg):
    # compute indicators once; ATR included when adx_threshold>0
    d = calculate_indicators(df5.copy(), cfg)
    h = calculate_htf_indicators(df_htf.copy(), cfg) if df_htf is not None and cfg.htf_enabled else None
    out = []
    for name, sl_spec in make_levels(cfg):
        out.append({"setting": name, **run_level(d, h, cfg, sl_spec)})
    return out


async def main(args):
    symbols = args.symbols or autodetect_symbols()
    print(f"Range: {args.start}..{args.end} | pairs={len(symbols)} | variants=wide,scalper1,scalper2,mid (TP=2xSL)", flush=True)
    api_key = os.getenv("BINANCE_API_KEY", ""); api_sec = os.getenv("BINANCE_API_SECRET", "")
    client = await AsyncClient.create(api_key=api_key or None, api_secret=api_sec or None)
    results = []; t0 = time.time()
    try:
        for k, sym in enumerate(symbols, 1):
            symfile = f"config_{sym.replace('USDT','').lower()}.yaml"
            cpath = BOT / symfile
            if not cpath.exists():
                print(f"[{k}/{len(symbols)}] {sym}: no config, skip", flush=True); continue
            cfg = load_config(str(cpath)); cfg.mode = "backtest"
            cfg.backtest_start = args.start; cfg.backtest_end = args.end
            tag = f"[{k}/{len(symbols)}] {sym}"
            try:
                print(f"{tag}: downloading 5m+1h ...", flush=True)
                df5 = await download(client, sym, cfg.timeframe, args.start, args.end)
                df_htf = await download(client, sym, cfg.htf_timeframe, args.start, args.end) if cfg.htf_enabled else None
                res = await asyncio.to_thread(analyze_sync, df5, df_htf, cfg)
                results.append({"symbol": sym, "candles_5m": len(df5), "settings": res})
                w = next(r for r in res if r["setting"] == "wide")
                s1 = next(r for r in res if r["setting"] == "scalper1")
                print(f"{tag}: wide  n={w['trades']} WR={w['win_rate']}% hold={w['avg_hold_min']}m | "
                      f"scalp1 n={s1['trades']} WR={s1['win_rate']}% hold={s1['avg_hold_min']}m | {time.time()-t0:.0f}s", flush=True)
            except Exception as e:
                print(f"{tag}: ERROR {e}", flush=True)
    finally:
        await client.close_connection()

    out = {"range": f"{args.start}..{args.end}",
           "scheme": "signal fixed per pair; SL/TP varied; TP=2xSL; expectancy per SL-unit",
           "pairs": results, "generated_at": pd.Timestamp.now("UTC").isoformat()}
    outp = BOT / "logs" / "diag_scalper_results.json"
    os.makedirs(outp.parent, exist_ok=True)
    with open(outp, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False, default=str)
    print(f"\nDONE ({time.time()-t0:.0f}s). Results -> {outp}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--start", default="2026-07-01")
    ap.add_argument("--end", default="2026-08-12")
    a = ap.parse_args()
    if a.symbols:
        a.symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    asyncio.run(main(a))
