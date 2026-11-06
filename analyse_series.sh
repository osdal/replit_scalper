#!/bin/bash
echo "=== bot demand per minute (sum over 40 bots), last 3h ==="
python3 - <<'PY'
import glob, re, collections, datetime
per=collections.Counter(); req=collections.Counter(); files=0
for f in glob.glob("/opt/trading-bot/bot/logs/testnet/api_*_stdout.log"):
    files+=1
    try: lines=open(f,errors="ignore").read().splitlines()
    except Exception: continue
    for ln in lines:
        if "[RATE_LIMIT] demand" not in ln: continue
        m=re.search(r"^\[(\d\d):(\d\d):(\d\d)", ln)
        d=re.search(r"req=(\d+) w=(\d+)", ln)
        if not (m and d): continue
        key=m.group(1)+":"+m.group(2)
        per[key]+=int(d.group(2)); req[key]+=int(d.group(1))
if not per:
    print("no demand lines"); raise SystemExit
ks=sorted(per)
print("files:", files, "minutes:", len(ks))
top=sorted(per.items(), key=lambda kv:-kv[1])[:15]
print("TOP 15 minutes by bot weight:")
for k,v in top:
    print(f"  {k}  weight={v:>5}  req={req[k]:>4}")
vals=sorted(per.values())
print("weight/min median/p90/max:", vals[len(vals)//2], vals[int(len(vals)*0.9)], vals[-1])
print()
print("last 20 minutes:")
for k in ks[-20:]:
    print(f"  {k}  weight={per[k]:>5} req={req[k]:>4}")
PY
echo
echo "=== api-testnet log volume per minute (grid/engine) last 2h ==="
docker logs replit_scalper-api-testnet --since 2h 2>&1 | grep -E "grid-orders|grid-engine" | awk -F'[][]' '{print substr($2,1,5)}' | sort | uniq -c | sort -rn | head -10
echo
echo "=== api-testnet: any burst around 06:00-06:10 ==="
docker logs replit_scalper-api-testnet --since 30m 2>&1 | grep -E "06:0[0-9]" | awk -F'[][]' '{print substr($2,1,5)}' | sort | uniq -c | head -20
