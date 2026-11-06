#!/bin/bash
date -u
echo "=== last 5 real -1003 events ==="
grep -h -E "^2026-11-06 [0-9]{2}:[0-9]{2}:[0-9]{2}.*IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | sort | tail -5
echo
echo "=== count bans since container start (06:49) ==="
grep -h -E "^2026-11-06 (06:49|06:5[0-9]|0[7-9]:[0-9]{2}):[0-9]{2}.*IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | wc -l
echo
echo "=== active ban in gate files ==="
python3 - <<'PY'
import glob,json,time
now=time.time()*1000; act=[]
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f))
        if d.get("ban_until_ms",0)>now: act.append((f.split(".")[-1], (d["ban_until_ms"]-now)/1000))
    except Exception: pass
print("active bans:",len(act), act[:3])
PY
echo
echo "=== last GateBlocked ==="
grep -h -E "^2026-11-06 [0-9]{2}:[0-9]{2}:[0-9]{2}.*GateBlocked" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | sort | tail -2
echo
echo "=== container ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'
