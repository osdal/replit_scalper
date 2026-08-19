#!/usr/bin/env python3
"""
Measure the historical effect of ATR-trailing (+ optional partial close) vs the
fixed 2xSL / full-close TP, using the same scalper2-style narrow levels.

Same signal as live (EMA cross + volume + HTF + ADX), fixed per pair.
Level baseline: SL = 0.7 x ATR(14), TP = 2 x SL (full close) — the current
scalper2 scheme.

Modes (SL/TP distances in units of `sl_dist0` = 0.7*ATR at entry):
  fixed      : exit at SL (=-1) or TP (=+2), full close.
  trail1.0   : SL trails at 1.0 x ATR below highest-high / above lowest-low;
               no TP (rides the trend), only a safety cap at +6 x SL.
  trail1.5   : SL trails at 1.5 x ATR (looser, fewer stop-outs by noise).
  trail50    : close 50% at first +1 x SL step, move SL to breakeven, trail
               the remaining 50% at 1.0 x ATR.

PnL is measured in SL-units: realized = (exit - entry)/sl_dist0 (signed).
So a TP at +2 x SL = +2 units, SL = -1, safety TP +6 = +6, breakeven ~0.
Metrics: trades, win_rate, expectancy/SL, avg win & avg loss (SL units),
avg hold (min), % closed within 30/60/120 min, total edge (SL-units).

Results -> bot/logs/diag_trail_results.json
Usage:
    cd bot
    python diag_trail.py
    python diag_trail.py --symbols INJUSDT,BNBUSDT,ETHUSDT
    python diag_trail.py --start 2026-07-01 --end 2026-08-12
"""
import sys, os, asyncio, json, argparse, logging, shutil, subprocess, time
from copy import deepcopy
from pathlib import Path
import numpy as np

BOT = Path(__file__).parent.resolve(); sys.path.insert(0, str(BOT))
try:
    import pandas as pd
except ImportError:
    print("pandas/numpy not found. searching python with both...", flush=True)
    found = None
    for base in [os.path.dirname(sys.executable),
                 r"C:\Users\osdal\AppData\Local\Programs\Python\Python311",
                 r"C:\Users\osdal\AppData\Local\Programs\Python\Python314", ""]:
        exe = os.path.join(base, "python.exe") if base else shutil.which("python")
        if not exe or not os.path.exists(exe): continue
        try:
            if subprocess.run([exe, "-c", "import pandas, numpy"], capture_output=True, timeout=30).returncode == 0:
                found = exe; break
        except Exception: pass
    if found:
        print(f"re-running with {found}", flush=True); os.execv(found, [found] + sys.argv)
    else:
        print('ERROR: no python with pandas+numpy. Use Python311 explicitly.'); sys.exit(1)

from dotenv import load_dotenv
load_dotenv()
from binance import AsyncClient
from config import load_config
from market_data import get_historical_klines
from strategy import calculate_indicators, calculate_htf_indicators, get_signal, get_htf_trend_latest

logging.getLogger("bot").setLevel(logging.CRITICAL)
MODES = ["fixed", "trail1.0", "trail1.5", "trail50"]
SL_K = 0.7            # SL = 0.7 x ATR
SAFETY_TP = 6.0       # safety cap, in SL units

def autodetect_symbols():
    syms = []
    for f in os.listdir(BOT):
        if f.startswith("config_") and f.endswith(".yaml") and f != "recovery_config.yaml":
            syms.append(f.replace("config_","").replace(".yaml","").upper() + "USDT")
    return sorted(set(syms))

async def download(client, sym, interval, start, end):
    return await get_historical_klines(client, symbol=sym, interval=interval, start=start, end=end)

def run_mode(df, df_htf, cfg, mode):
    closes = df["close"].to_numpy(float)
    highs = df["high"].to_numpy(float)
    lows = df["low"].to_numpy(float)
    atr = df["atr"].to_numpy(float) if "atr" in df.columns else None

    trail_k = 1.0
    if mode == "trail1.5": trail_k = 1.5
    partial = (mode == "trail50")
    trailing = mode != "fixed"

    # entry = dict(side, i, eprice, sl0)
    entry = None
    trades=wins=0
    hold=[]; pnls=[]
    c30=c60=c120=0

    for i in range(1, len(df)):
        hi=highs[i]; lo=lows[i]; px=closes[i]
        if entry is not None:
            side=entry["side"]; eprice=entry["eprice"]; sl0=entry["sl0"]
            hit=None
            if not trailing:
                slv = eprice - sl0 if side=="LONG" else eprice + sl0
                tpv = eprice + 2*sl0 if side=="LONG" else eprice - 2*sl0
                if side=="LONG":
                    if lo <= slv: hit=("SL", slv)
                    elif hi >= tpv: hit=("TP", tpv)
                else:
                    if hi >= slv: hit=("SL", slv)
                    elif lo <= tpv: hit=("TP", tpv)
            else:
                # trailing extreme = max high (LONG) / min low (SHORT)
                if side=="LONG":
                    entry["extr"] = max(entry.get("extr", eprice), hi)
                    # SL trails at trail_k*ATR from extreme; after +1xSL step -> breakeven
                    trail_sl = entry["extr"] - trail_k*sl0
                    if (entry["extr"] - eprice) >= sl0:
                        trail_sl = max(trail_sl, eprice)          # breakeven floor
                    safety = eprice + SAFETY_TP*sl0
                    if lo <= trail_sl: hit=("SL", trail_sl)
                    elif hi >= safety: hit=("TP", safety)
                else:
                    entry["extr"] = min(entry.get("extr", eprice), lo)
                    trail_sl = entry["extr"] + trail_k*sl0
                    if (eprice - entry["extr"]) >= sl0:
                        trail_sl = min(trail_sl, eprice)
                    safety = eprice - SAFETY_TP*sl0
                    if hi >= trail_sl: hit=("SL", trail_sl)
                    elif lo <= safety: hit=("TP", safety)

            if hit:
                kind, exit_price = hit
                # realized pnl in SL units (signed)
                move = (exit_price - eprice) if side=="LONG" else (eprice - exit_price)
                pnl_sl = move / sl0 if sl0>0 else 0.0
                # partial (trail50): 50% closed at first +1xSL step, tail at pnl_sl
                if partial and kind=="SL" and pnl_sl > 0:
                    # tail closed at breakeven-ish (trail moved to eprice) -> ~0 for tail
                    pnl_sl = 1.0   # realized: first half +1, tail ~0 -> avg +0.5? We treat whole position:
                    # We'll represent per-trade; note partial ~half the size. Keep simple but consistent:
                    pnl_sl = 0.5   # approximate: 50% at +1, 50% at ~0
                trades+=1
                hold.append(i-entry["i"])
                pnls.append(pnl_sl)
                if pnl_sl > 0: wins+=1
                h=i-entry["i"]; 
                if h<=6: c30+=1
                if h<=12: c60+=1
                if h<=24: c120+=1
                entry=None
            continue

        htf_trend = get_htf_trend_latest(df_htf) if df_htf is not None else None
        sig = get_signal(df.iloc[:i+1], cfg, htf_trend=htf_trend)
        if sig is None: continue
        eprice = sig.entry_price
        # sl0 from ATR at this bar
        base = 0.7*(atr[i]/eprice) if atr is not None and atr[i]>0 else cfg.sl_pct/100.0*0.7
        sl0 = max(eprice*base, eprice*0.00005)   # floor tiny
        entry={"side":sig.direction, "i":i, "eprice":eprice, "sl0":sl0, "extr":eprice}

    pnls=np.array(pnls) if pnls else np.array([0.0])
    n=trades
    wr=wins/n*100 if n else 0
    hold=np.array(hold) if hold else np.array([0.0])
    wins_arr = pnls[pnls>0]; losses = pnls[pnls<=0]
    return {
        "mode":mode, "trades":n, "win_rate":round(wr,1),
        "expectancy_per_sl":round(float(pnls.mean()),4) if n else 0,
        "total_edge_sl":round(float(pnls.sum()),1),
        "avg_win_sl":round(float(wins_arr.mean()),3) if len(wins_arr) else 0,
        "avg_loss_sl":round(float(losses.mean()),3) if len(losses) else 0,
        "avg_hold_min":round(float(hold.mean())*5,1),
        "pct_closed_30min":round(100*c30/n,1) if n else 0,
        "pct_closed_60min":round(100*c60/n,1) if n else 0,
        "pct_closed_120min":round(100*c120/n,1) if n else 0,
    }

def analyze_sync(df5, df_htf, cfg):
    d = calculate_indicators(df5.copy(), cfg)
    h = calculate_htf_indicators(df_htf.copy(), cfg) if df_htf is not None and cfg.htf_enabled else None
    return [run_mode(d, h, cfg, m) for m in MODES]

async def main(args):
    symbols = args.symbols or autodetect_symbols()
    print(f"Range: {args.start}..{args.end} | pairs={len(symbols)} | modes={MODES}", flush=True)
    k=os.getenv("BINANCE_API_KEY",""); s=os.getenv("BINANCE_API_SECRET","")
    client = await AsyncClient.create(api_key=k or None, api_secret=s or None)
    results=[]; t0=time.time()
    try:
        for idx, sym in enumerate(symbols,1):
            symfile=f"config_{sym.replace('USDT','').lower()}.yaml"
            cpath=BOT/symfile
            if not cpath.exists():
                print(f"[{idx}/{len(symbols)}] {sym}: no config, skip", flush=True); continue
            cfg=load_config(str(cpath)); cfg.mode="backtest"; cfg.backtest_start=args.start; cfg.backtest_end=args.end
            tag=f"[{idx}/{len(symbols)}] {sym}"
            try:
                print(f"{tag}: downloading...", flush=True)
                df5=await download(client, sym, cfg.timeframe, args.start, args.end)
                df_htf=await download(client, sym, cfg.htf_timeframe, args.start, args.end) if cfg.htf_enabled else None
                res=await asyncio.to_thread(analyze_sync, df5, df_htf, cfg)
                results.append({"symbol":sym, "candles_5m":len(df5), "modes":res})
                fix=next(r for r in res if r["mode"]=="fixed")
                tr=next(r for r in res if r["mode"]=="trail1.0")
                print(f"{tag}: fixed n={fix['trades']} exp={fix['expectancy_per_sl']:+.3f} | trail1.0 n={tr['trades']} exp={tr['expectancy_per_sl']:+.3f} | {time.time()-t0:.0f}s", flush=True)
            except Exception as e:
                print(f"{tag}: ERROR {e}", flush=True)
    finally:
        await client.close_connection()
    out={"range":f"{args.start}..{args.end}",
         "scheme":f"scalper2 levels (SL=0.7xATR, TP=2xSL fixed) vs ATR-trailing({MODES[1]}/{MODES[2]}/50%partial); PnL in SL-units; safety TP cap={SAFETY_TP:.0f}xSL",
         "pairs":results, "generated_at":pd.Timestamp.now("UTC").isoformat()}
    outp=BOT/"logs"/"diag_trail_results.json"
    os.makedirs(outp.parent, exist_ok=True)
    with open(outp,"w",encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, ensure_ascii=False, default=str)
    print(f"\nDONE ({time.time()-t0:.0f}s). Results -> {outp}", flush=True)

if __name__=="__main__":
    ap=argparse.ArgumentParser()
    ap.add_argument("--symbols", default=None)
    ap.add_argument("--start", default="2026-07-01")
    ap.add_argument("--end", default="2026-08-12")
    a=ap.parse_args()
    if a.symbols: a.symbols=[s.strip().upper() for s in a.symbols.split(",") if s.strip()]
    asyncio.run(main(a))
