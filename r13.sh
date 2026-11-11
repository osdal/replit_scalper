#!/bin/bash
date -u
echo "=== logs 09:20-09:30 (ban boundary) ==="
grep -hE "^2026-11-10 09:(2[0-9]|30)" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null \
  | grep -E "ban|warm-up|SYNC|Candle closed" | cut -c1-150 | head -25
echo
echo "=== first ban record after 09:24 ==="
grep -hE "^2026-11-10 09:(2[4-9])" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null \
  | grep -iE "banned|no block|-1003" | cut -c1-150 | head -12
echo
echo "=== who wrote ban (files with ban in last 24h) ==="
python3 - << 'EOF'
import glob, json, time, datetime
now = time.time()
for f in glob.glob('/opt/trading-bot/bot/data/binance_gate_testnet.json.*'):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    ban = d.get('ban_until_ms', 0)
    if ban and ban > now*1000:
        print(f.rsplit('.',1)[-1], 'ban until', datetime.datetime.utcfromtimestamp(ban/1000).strftime('%H:%M:%S'), 'file age=%.0fs' % (now-d.get('ts',0)))
EOF
