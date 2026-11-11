#!/bin/bash
date -u
echo ""
echo "=== gate file with active ban ==="
python3 << 'EOF'
import glob,json,time,datetime
now=time.time()
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f))
        ban=d.get("ban_until_ms",0)
        if ban>now*1000:
            print(f"{f}: ban_until={ban} ({datetime.datetime.utcfromtimestamp(ban/1000).strftime('%H:%M:%S')} UTC), left={(ban-now*1000)/60000:.1f} min")
    except: pass
EOF
echo ""
echo "=== last real Binance bans (stdout) ==="
grep -h "Way too many requests" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | tail -3
echo ""
echo "=== ban events since 12:35 UTC ==="
grep -h -E "^2026-11-07 (1[2-9]):[0-9]{2}:[0-9]{2}.*IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -5
echo ""
echo "=== total local weight (fresh <=90s) ==="
python3 << 'EOF'
import glob,json,time
now=time.time(); tot=0; n=0
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f))
        if now-d.get("ts",0)<=90:
            n+=1; tot+=int(d.get("local_weight_1m",0) or 0)
    except: pass
print(f"fresh={n} total_local_weight={tot} shed_threshold=1200")
EOF
echo ""
echo "=== GateBlocked last 20 min ==="
grep -h "GateBlocked" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | grep -E "1[2-3]:[0-9]{2}:[0-9]{2}" | tail -3
