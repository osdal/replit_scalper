#!/bin/bash
date -u
echo "=== new code in image? ==="
docker exec replit_scalper-api-testnet bash -lc "grep -c 'skipping cycle' /app/bot/rate_limit.py; grep -c 'CANDLE_QUEUE_MAXSIZE' /app/bot/market_data.py"
echo ""
echo "=== bots running ==="
docker exec replit_scalper-api-testnet bash -lc "ps aux | grep python3 | wc -l"
echo ""
echo "=== ban-active gate files ==="
python3 << 'EOF'
import glob,json,time
now=time.time(); act=[]
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f))
        if d.get("ban_until_ms",0)>now*1000 and now-d.get("ts",0)<=90:
            act.append((f.split(".")[-1], round((d["ban_until_ms"]-now*1000)/60000,1)))
    except: pass
print("active:", len(act), act[:8])
EOF
echo ""
echo "=== 'no block' skip lines since restart ==="
grep -h "skipping cycle" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -5
echo ""
echo "=== queue overflows since 13:27 ==="
grep -h "QueueOverflow" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | wc -l
echo ""
echo "=== last lines: a few bots ==="
for s in btc sui op apt; do echo "--- $s"; tail -3 /opt/trading-bot/bot/logs/testnet/api_${s}usdt_stdout.log 2>/dev/null; done
