#!/bin/bash
sleep 150
echo "=== total local weight across gate files ==="
python3 - <<'PY'
import glob,json,time
now=time.time(); tot=0; n=0; hdr=[]
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f))
        if now-d.get("ts",0)<=60:
            tot+=int(d.get("local_weight_1m",0) or 0); n+=1
            hdr.append(int(d.get("used_weight_1m",0) or 0))
    except Exception: pass
print(f"fresh files={n} local_sum={tot}  (shed threshold={int(2400*0.5)})")
if hdr: print("header values min/med/max:", min(hdr), sorted(hdr)[len(hdr)//2], max(hdr))
PY
echo
echo "=== demand lines since 06:37 (shed counts) ==="
grep -h "RATE_LIMIT] demand" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -6
echo
echo "=== stats lines with local/effective ==="
grep -h "RATE_LIMIT] used_weight" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -5
echo
echo "=== bans after deploy (06:37+) ==="
grep -h "IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | awk '$0 >= "2026-11-06 06:37"' | tail -5
echo
echo "=== GateBlocked after deploy ==="
grep -h "GateBlocked" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | awk '$0 >= "2026-11-06 06:37"' | tail -5
echo
echo "=== ticker REST failures after deploy ==="
grep -h "REST ticker failed" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | awk '$0 >= "2026-11-06 06:37"' | tail -5
echo
echo "=== container status ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'
