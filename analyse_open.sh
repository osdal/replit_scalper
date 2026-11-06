#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect("data/bot.db"); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
def dt(s):
    try: d = datetime.datetime.fromisoformat(str(s).replace("Z","+00:00"))
    except Exception: return None
    if d.tzinfo is None: d = d.replace(tzinfo=datetime.timezone.utc)
    return d
print("=== OPEN ROWS FULL ===")
for r in con.execute("SELECT * FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY id"):
    d=dict(r)
    a=dt(d["entry_time"]); age=(now-a).total_seconds()/3600 if a else -1
    print(f"#{d['id']} {d['symbol']} {d['direction']} preset={d['preset']} age={age:.2f}h chain_depth={d['chain_depth']} leg_pnl={d['leg_pnl']} cycle_pnl={d['cycle_pnl']} status={d['status']} mode={d['mode']}")
    print(f"   entry_time={d['entry_time']} entry={d['entry_price']} qty={d['qty']} sl={d['sl_price']} tp1={d['tp1_price']} cycle_close_reason={d['cycle_close_reason']}")
print()
print("=== ALL ROWS for TAO/TON/WLD since Nov 5 ===")
for sym in ("TAOUSDT","TONUSDT","WLDUSDT"):
    print(f"--- {sym} ---")
    for r in con.execute("SELECT id,direction,entry_price,exit_price,pnl,exit_reason,entry_time,exit_time,is_open,status,chain_depth FROM trades WHERE symbol=? AND entry_time>='2026-11-05' ORDER BY id", (sym,)):
        d=dict(r)
        print(f"  #{d['id']} {d['direction']} entry={d['entry_price']} exit={d['exit_price']} pnl={d['pnl']} reason={d['exit_reason']} open={d['is_open']} st={d['status']} chain={d['chain_depth']} in={d['entry_time']} out={d['exit_time']}")
print()
print("=== BOTS positions raw + updated_at ===")
for r in con.execute("SELECT symbol,position,updated_at,last_heartbeat FROM bots WHERE position IS NOT NULL AND position!=''"):
    print(f"{r['symbol']:<11} updated={r['updated_at']} hb={r['last_heartbeat']}")
    print("   ", str(r["position"])[:200])
print()
print("=== trading_control ===")
for r in con.execute("SELECT * FROM trading_control"):
    print(dict(r))
print()
print("=== recovery_chains ===")
for r in con.execute("SELECT * FROM recovery_chains ORDER BY rowid DESC LIMIT 10"):
    print(dict(r))
PY
