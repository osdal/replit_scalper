#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3, datetime, statistics
con = sqlite3.connect("data/bot.db"); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)

def dt(s):
    if not s: return None
    try:
        d = datetime.datetime.fromisoformat(str(s).replace("Z","+00:00"))
    except Exception:
        return None
    if d.tzinfo is None: d = d.replace(tzinfo=datetime.timezone.utc)
    return d

since = (now - datetime.timedelta(hours=24)).isoformat()
print("NOW:", now.strftime("%Y-%m-%d %H:%M UTC"))
print("WINDOW:", since, "->", now.isoformat())
print()

rows = [dict(r) for r in con.execute(
    "SELECT * FROM trades WHERE is_open=0 AND exit_time IS NOT NULL AND exit_time >= ? ORDER BY exit_time", (since,))]
rows = [r for r in rows if r.get("status") != "rejected" and r.get("mode") != "paper"]

print("=== 1. CLOSED TRADES (24h) ===")
print("total closed:", len(rows))
if rows:
    pnl = [float(r["pnl"] or 0) for r in rows]
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    zeros = [p for p in pnl if p == 0]
    print(f"net PnL      : {sum(pnl):+.4f}")
    print(f"gross profit : {sum(wins):+.4f}   gross loss: {sum(losses):+.4f}")
    print(f"wins {len(wins)} | losses {len(losses)} | zero {len(zeros)}")
    if wins or losses:
        print(f"winrate      : {len(wins)/(len(wins)+len(losses))*100:.1f}%")
    if wins: print(f"avg win      : {sum(wins)/len(wins):+.4f}")
    if losses: print(f"avg loss     : {sum(losses)/len(losses):+.4f}")
    if losses and sum(losses) != 0:
        print(f"profit factor: {sum(wins)/abs(sum(losses)):.2f}")
    holds = []
    for r in rows:
        a, b = dt(r["entry_time"]), dt(r["exit_time"])
        if a and b: holds.append((b-a).total_seconds()/3600)
    if holds:
        holds.sort()
        print(f"hold hours   : avg {statistics.mean(holds):.2f} | median {statistics.median(holds):.2f} | max {max(holds):.2f}")
print()

print("=== 2. BY EXIT REASON ===")
agg = {}
for r in rows:
    k = r["exit_reason"] or "(none)"
    a = agg.setdefault(k, [0, 0.0, 0, 0])
    p = float(r["pnl"] or 0)
    a[0] += 1; a[1] += p
    if p > 0: a[2] += 1
    elif p < 0: a[3] += 1
for k, v in sorted(agg.items(), key=lambda x: x[1][1]):
    print(f"{k:<28} n={v[0]:<4} net={v[1]:+9.4f}  w={v[2]:<3} l={v[3]}")
print()

print("=== 3. BY SYMBOL ===")
agg = {}
for r in rows:
    k = r["symbol"]
    a = agg.setdefault(k, [0, 0.0, 0, 0])
    p = float(r["pnl"] or 0)
    a[0] += 1; a[1] += p
    if p > 0: a[2] += 1
    elif p < 0: a[3] += 1
for k, v in sorted(agg.items(), key=lambda x: x[1][1]):
    print(f"{k:<12} n={v[0]:<3} net={v[1]:+9.4f}  w={v[2]:<3} l={v[3]}")
print()

print("=== 4. DIRECTIONS ===")
for d in ("LONG","SHORT"):
    sub = [float(r["pnl"] or 0) for r in rows if r["direction"] == d]
    if sub:
        print(f"{d:<6} n={len(sub):<3} net={sum(sub):+.4f} wins={len([x for x in sub if x>0])}")
print()

print("=== 5. REVERSE / CHAIN ===")
rev = [r for r in rows if (r.get("chain_depth") or 0) > 0 or "REVERSE" in (r.get("exit_reason") or "")]
plain = [r for r in rows if r not in rev]
print(f"reverse-related: n={len(rev)} net={sum(float(r['pnl'] or 0) for r in rev):+.4f}")
print(f"plain          : n={len(plain)} net={sum(float(r['pnl'] or 0) for r in plain):+.4f}")
depths = {}
for r in rows:
    d = r.get("chain_depth") or 0
    a = depths.setdefault(d, [0,0.0])
    a[0]+=1; a[1]+=float(r["pnl"] or 0)
for d in sorted(depths):
    print(f"  chain_depth={d}: n={depths[d][0]:<3} net={depths[d][1]:+.4f}")
print()

print("=== 6. DAILY NET (last 5 days) ===")
for r in con.execute("""SELECT substr(exit_time,1,10) d, COUNT(*) n, SUM(pnl) net,
        SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) w, SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) l
        FROM trades WHERE is_open=0 AND status!='rejected' AND exit_time IS NOT NULL
        GROUP BY substr(exit_time,1,10) ORDER BY d DESC LIMIT 5"""):
    print(f"{r['d']}  n={r['n']:<4} net={float(r['net'] or 0):+9.4f}  w={r['w']} l={r['l']}")
print()

print("=== 7. OPENS (24h) ===")
op = [dict(r) for r in con.execute(
    "SELECT * FROM trades WHERE entry_time >= ? AND status!='rejected' ORDER BY entry_time", (since,))]
print("opened:", len(op))
rej = con.execute("SELECT COUNT(*) FROM trades WHERE entry_time >= ? AND status='rejected'", (since,)).fetchone()[0]
print("rejected:", rej)
print()

print("=== 8. OPEN POSITIONS NOW ===")
for r in con.execute("SELECT * FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time"):
    a = dt(r["entry_time"])
    age = (now - a).total_seconds()/3600 if a else -1
    print(f"{r['symbol']:<11} {r['direction']:<5} age={age:5.1f}h qty={r['qty']} entry={r['entry_price']} chain_depth={r.get('chain_depth')} preset={r.get('preset')}")
print()

print("=== 9. HOURLY PNL (24h) ===")
hagg = {}
for r in rows:
    b = dt(r["exit_time"])
    if not b: continue
    k = b.strftime("%m-%d %H")
    a = hagg.setdefault(k, [0,0.0])
    a[0]+=1; a[1]+=float(r["pnl"] or 0)
for k in sorted(hagg):
    n, p = hagg[k]
    bar = "#" * min(40, int(abs(p)*200))
    print(f"{k}h n={n:<3} {p:+8.4f} {bar}")
print()

print("=== 10. BOTS STATE ===")
tot = 0; armed = 0; recent = 0; stuck = []
for r in con.execute("SELECT symbol,is_running,armed,last_heartbeat,position,stop_reason FROM bots"):
    tot += 1
    if r["armed"]: armed += 1
    hb = dt(r["last_heartbeat"])
    if hb and (now-hb).total_seconds() < 300: recent += 1
    if r["position"]: stuck.append((r["symbol"], r["position"]))
print(f"bots={tot} armed={armed} heartbeat<5min={recent}")
print("bots with position field:", stuck if stuck else "none")
PY
