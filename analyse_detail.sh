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

rows = [dict(r) for r in con.execute(
    "SELECT * FROM trades WHERE is_open=0 AND exit_time IS NOT NULL AND exit_time >= ? AND status!='rejected' ORDER BY exit_time", (since,))]

print("=== CLOSED TRADES (24h) DETAIL ===")
print(f"{'id':<8}{'sym':<11}{'dir':<6}{'preset':<20}{'hold':>6}{'pnl':>9}  reason")
for r in rows:
    a,b = dt(r["entry_time"]), dt(r["exit_time"])
    hold = (b-a).total_seconds()/3600 if a and b else -1
    print(f"{r['id']:<8}{r['symbol']:<11}{r['direction']:<6}{(r['preset'] or '-')[:19]:<20}{hold:>6.1f}{float(r['pnl'] or 0):>+9.4f}  {r['exit_reason']}")
print()

print("=== BY PRESET ===")
agg={}
for r in rows:
    k=r["preset"] or "-"
    a=agg.setdefault(k,[0,0.0,0,0])
    p=float(r["pnl"] or 0); a[0]+=1; a[1]+=p
    if p>0: a[2]+=1
    elif p<0: a[3]+=1
for k,v in sorted(agg.items(), key=lambda x:-x[1][1]):
    print(f"{k:<22} n={v[0]:<3} net={v[1]:+8.4f} w={v[2]} l={v[3]}")
print()

print("=== TP1 with negative pnl ===")
for r in rows:
    if r["exit_reason"]=="TP1" and float(r["pnl"] or 0)<0:
        a,b = dt(r["entry_time"]), dt(r["exit_time"])
        print(f"#{r['id']} {r['symbol']} {r['direction']} entry={r['entry_price']} exit={r['exit_price']} pnl={r['pnl']} hold={(b-a).total_seconds()/3600 if a and b else -1:.1f}h tp1={r['tp1_price']} preset={r['preset']}")
print()

print("=== EQUITY CURVE (cumulative pnl, all closed trades, last 3 days) ===")
c=0.0
for r in con.execute("""SELECT exit_time, pnl, symbol FROM trades WHERE is_open=0 AND status!='rejected'
                        AND exit_time >= ? ORDER BY exit_time""", ((now-datetime.timedelta(days=3)).isoformat(),)):
    c += float(r["pnl"] or 0)
    print(f"{str(r['exit_time'])[:16]}  {r['symbol']:<10} {float(r['pnl'] or 0):+8.4f}  cum={c:+8.4f}")
print(f"TOTAL cum: {c:+.4f}")
print()

print("=== MAX DRAWDOWN (3 days) ===")
peak=0.0; mdd=0.0; c=0.0
for r in con.execute("""SELECT pnl FROM trades WHERE is_open=0 AND status!='rejected'
                        AND exit_time >= ? ORDER BY exit_time""", ((now-datetime.timedelta(days=3)).isoformat(),)):
    c += float(r["pnl"] or 0)
    peak=max(peak,c); mdd=min(mdd, c-peak)
print(f"max drawdown: {mdd:+.4f} USD")
print()

print("=== 24h: hold time buckets ===")
buck={"<0.5h":0,"0.5-2h":0,"2-6h":0,"6h+":0}
for r in rows:
    a,b=dt(r["entry_time"]),dt(r["exit_time"])
    if not(a and b): continue
    h=(b-a).total_seconds()/3600
    k="<0.5h" if h<0.5 else "0.5-2h" if h<2 else "2-6h" if h<6 else "6h+"
    buck[k]+=1
for k,v in buck.items(): print(f"{k:<8} {v}")
PY
