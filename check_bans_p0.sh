#!/bin/bash
date -u
echo "=== bans since P0 deploy (06:49 UTC) ==="
grep -h -E "^2026-11-06 (06:(4[9-9]|5[0-9]):[0-9]{2}|0[7-9]:[0-9]{2}:[0-9]{2}).*IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | sort | tail -10
echo ""
echo "=== count bans by hour (today) ==="
grep -h -oE "^2026-11-06 [0-9]{2}:[0-9]{2}:[0-9]{2}.*IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | cut -c1-13 | sort | uniq -c | tail -8
echo ""
echo "=== active bans in gate files ==="
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
echo ""
echo "=== GateBlocked after deploy ==="
grep -h -E "^2026-11-06 (06:(4[9-9]|5[0-9]):[0-9]{2}|0[7-9]:[0-9]{2}:[0-9]{2}).*GateBlocked" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | sort | tail -3
echo ""
echo "=== container ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'