#!/bin/bash
cd /opt/trading-bot
L=bot/logs/testnet
python3 - <<'PY'
import glob, re, os
L="bot/logs/testnet"
tot_req=0; tot_w=0; files=0; rows=[]
for f in glob.glob(os.path.join(L,"api_*_stdout.log")):
    try:
        lines=open(f, errors="ignore").read().splitlines()
    except Exception: continue
    last=None
    for ln in reversed(lines[-400:]):
        if "[RATE_LIMIT] demand" in ln:
            last=ln; break
    if not last: continue
    m=re.search(r"demand (\d+)s \| req=(\d+) w=(\d+) critical=(\d+) shed=(\d+)", last)
    if not m: continue
    files+=1
    tot_req+=int(m.group(2)); tot_w+=int(m.group(3))
    rows.append((os.path.basename(f).replace("api_","").replace("_stdout.log",""), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5)), int(m.group(1))))
print(f"bots with demand log: {files}")
print(f"SUM last window: req={tot_req} weight={tot_w}")
print()
rows.sort(key=lambda r:-r[2])
print(f"{'symbol':<14}{'req':>5}{'w':>6}{'crit':>6}{'shed':>6}{'window':>8}")
for r in rows[:15]:
    print(f"{r[0]:<14}{r[1]:>5}{r[2]:>6}{r[3]:>6}{r[4]:>6}{r[5]:>8}")
print("...")
print(f"avg window sec: {sum(r[5] for r in rows)/len(rows):.1f}" if rows else "")
PY
echo
echo "=== used_weight samples (10s x 6) ==="
for i in 1 2 3 4 5 6; do
  python3 - <<'PY'
import glob, json
vals=[]
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f)); t=d.get("ts",0)
        if t>0: vals.append((t,int(d.get("used_weight_1m") or 0)))
    except Exception: pass
vals.sort()
recent=[v for v in vals if v[0] > vals[-1][0]-120] if vals else []
print("max_weight_last2m:", max((w for _,w in recent), default=0), "files:", len(recent))
PY
  sleep 10
done
echo
echo "=== api container binance traffic (last 10m) ==="
docker logs replit_scalper-api-testnet --since 10m 2>&1 | grep -icE "binance|fapi" || echo 0
echo
echo "=== other containers on same IP ==="
docker ps --format '{{.Names}}' | grep -i replit
