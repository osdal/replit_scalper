#!/bin/bash
date -u
echo "=== ban-active gate files (fresh <90s) ==="
python3 << 'EOF'
import glob,json,time
now=time.time(); act=[]; fresh=0
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d=json.load(open(f))
        if now-d.get("ts",0)<=90:
            fresh+=1
            if d.get("ban_until_ms",0)>now*1000:
                act.append((f.split(".")[-1], round((d["ban_until_ms"]-now*1000)/60000,1)))
    except Exception:
        pass
print("fresh:", fresh, "active_bans:", len(act), act[:5])
EOF
echo ""
echo "=== real Binance bans since 13:27 ==="
grep -h "Way too many requests" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "2026-11-07 1[3-9]:" | tail -5
echo "(empty above = no new bans)"
echo ""
echo "=== queue overflow delta ==="
base=$(cat /tmp/overflow_baseline.txt 2>/dev/null || echo 0)
cur=$(grep -h "QueueOverflow" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | wc -l)
echo "current=$cur baseline=$base delta=$((cur-base))"
echo ""
echo "=== bots with fresh candles after 13:48 ==="
alive=0
for f in /opt/trading-bot/bot/logs/testnet/*_stdout.log; do
  last=$(grep "Candle closed" "$f" 2>/dev/null | tail -1 | cut -c1-16)
  if [[ "$last" > "2026-11-07 13:48" ]]; then alive=$((alive+1)); fi
done
echo "alive_after_13:48=$alive / 40"
echo ""
echo "=== bots still stuck in warm-up (last warm-up fail after 13:40) ==="
grep -h "warm-up klines failed" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "1[3-9]:[0-9]{2}:[0-9]{2}" | tail -3
echo ""
echo "=== open positions in DB ==="
python3 -c "import sqlite3;c=sqlite3.connect('/opt/trading-bot/data/bot.db');print(c.execute('select symbol,direction,datetime(entry_time),status from trades where is_open=1').fetchall())"
echo ""
echo "=== restored state for open symbols ==="
for s in sui fil op apt; do
  echo -n "$s: "
  grep -h "Restored\|RESTORE" /opt/trading-bot/bot/logs/testnet/api_${s}usdt_stdout.log 2>/dev/null | tail -1
  echo ""
done
echo ""
echo "=== last skip lines (new wording = fix active) ==="
grep -h "skipping cycle" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | tail -3
