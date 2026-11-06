#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect("data/bot.db"); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
def dt(s):
    if not s: return None
    try: d = datetime.datetime.fromisoformat(str(s).replace("Z","+00:00"))
    except Exception: return None
    if d.tzinfo is None: d = d.replace(tzinfo=datetime.timezone.utc)
    return d
since = (now - datetime.timedelta(hours=24)).isoformat()

print("=== OPEN POSITIONS NOW ===")
for r in con.execute("SELECT * FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time"):
    d = dict(r)
    a = dt(d["entry_time"])
    age = (now-a).total_seconds()/3600 if a else -1
    print(f"{d['symbol']:<11} {d['direction']:<5} age={age:5.1f}h qty={d['qty']} entry={d['entry_price']} chain={d['chain_depth']} preset={d['preset']} mode={d['mode']} id={d['id']}")
print()

print("=== ETHUSDT LAST 3 DAYS ===")
for r in con.execute("""SELECT id,direction,qty,entry_price,exit_price,pnl,exit_reason,entry_time,exit_time,is_open,status,chain_depth,cycle_close_reason
                        FROM trades WHERE symbol='ETHUSDT' AND entry_time >= ? ORDER BY entry_time""",
                     ((now-datetime.timedelta(days=3)).isoformat(),)):
    d = dict(r)
    print(f"#{d['id']:<7} {d['direction']:<5} qty={d['qty']} entry={d['entry_price']} exit={d['exit_price']} pnl={d['pnl']} reason={d['exit_reason']} open={d['is_open']} status={d['status']} chain={d['chain_depth']} cyc={d['cycle_close_reason']}")
    print(f"        entry={d['entry_time']} exit={d['exit_time']}")
print()

print("=== REJECT REASONS (24h) ===")
for r in con.execute("""SELECT reject_reason, COUNT(*) n FROM trades
                        WHERE entry_time >= ? AND status='rejected' GROUP BY reject_reason ORDER BY n DESC LIMIT 15""", (since,)):
    print(f"{(r['reject_reason'] or '(null)')[:70]:<72} {r['n']}")
print()

print("=== CYCLE CLOSE REASONS (24h closed) ===")
for r in con.execute("""SELECT cycle_close_reason, COUNT(*) n, SUM(pnl) net FROM trades
                        WHERE is_open=0 AND exit_time >= ? AND status!='rejected'
                        GROUP BY cycle_close_reason ORDER BY n DESC""", (since,)):
    print(f"{(r['cycle_close_reason'] or '(null)'):<28} n={r['n']:<4} net={float(r['net'] or 0):+.4f}")
print()

print("=== ALL EXIT REASONS last 3 days (incl pnl=0 closes w/o exit_time) ===")
for r in con.execute("""SELECT exit_reason, COUNT(*) n, SUM(pnl) net FROM trades
                        WHERE is_open=0 AND status!='rejected' AND entry_time >= ?
                        GROUP BY exit_reason ORDER BY n DESC""", ((now-datetime.timedelta(days=3)).isoformat(),)):
    print(f"{(r['exit_reason'] or '(null)'):<28} n={r['n']:<4} net={float(r['net'] or 0):+.4f}")
print()

print("=== TRADES STILL OPEN (is_open=1) count by mode ===")
for r in con.execute("SELECT mode, COUNT(*) n FROM trades WHERE is_open=1 AND status!='rejected' GROUP BY mode"):
    print(f"{r['mode']}: {r['n']}")
print()

print("=== BOTS ===")
tot=armed=recent=0; pos=[]
for r in con.execute("SELECT symbol,is_running,armed,last_heartbeat,position,stop_reason,stop_requested,mode FROM bots"):
    tot+=1
    if r["armed"]: armed+=1
    hb=dt(r["last_heartbeat"])
    if hb and (now-hb).total_seconds()<300: recent+=1
    if r["position"]: pos.append((r["symbol"], str(r["position"])[:40]))
print(f"bots={tot} armed={armed} hb<5min={recent}")
print("with position:", pos if pos else "none")
stops = con.execute("SELECT COUNT(*) FROM bots WHERE stop_requested=1").fetchone()[0]
print("stop_requested:", stops)
PY
