#!/bin/bash
date -u
echo "=== python3 processes ==="
docker exec replit_scalper-api-testnet bash -lc "ps aux | grep python3 | grep -v grep | wc -l"
echo ""
echo "=== still running (DB) ==="
python3 << 'EOF'
import sqlite3
c=sqlite3.connect("/opt/trading-bot/data/bot.db")
run=[r[0] for r in c.execute("select symbol from bots where is_running=1 order by symbol")]
print(len(run), run)
EOF
echo ""
echo "=== ban-active fresh gate files ==="
python3 << 'EOF'
import glob,json,time,datetime
now=time.time(); act=[]
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f))
        if now-d.get("ts",0)<=120 and d.get("ban_until_ms",0)>now*1000:
            act.append((f.split(".")[-1], datetime.datetime.utcfromtimestamp(d["ban_until_ms"]/1000).strftime("%H:%M:%S")))
    except Exception: pass
print("active bans:", len(act), act[:5])
EOF
echo ""
echo "=== real bans since 13:47 ==="
grep -h "Way too many requests" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "2026-11-07 (1[3-9]):" | tail -4
echo ""
echo "=== candles after 13:55 ==="
alive=0
for f in /opt/trading-bot/bot/logs/testnet/*_stdout.log; do
  last=$(grep "Candle closed" "$f" 2>/dev/null | tail -1 | cut -c1-16)
  if [[ "$last" > "2026-11-07 13:55" ]]; then alive=$((alive+1)); fi
done
echo "bots with candles after 13:55: $alive"
