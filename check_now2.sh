#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== recent activity per bot (last 15 min) ==="
for s in 1000pepeusdt adausdt dogeusdt filusdt injusdt lskusdt nearusdt trxusdt; do
  f="bot/logs/testnet/api_${s}_stdout.log"
  echo "--- $s ---"
  grep -aE "2026-11-10 12:(4[0-9]|5[0-9])" "$f" 2>/dev/null | grep -avE '\[CACHE\]|\[RATE_LIMIT\] demand|IP banned -1003 \(same ban\)|post-ban resume' | tail -10
  echo
done
echo
echo "=== any signal/log activity recent ==="
grep -ah "New signal received\|REVERSE|SL hit\|TP1 hit\|TP2 hit" bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep "2026-11-10 12:5" | tail -20
echo
echo "=== errors last 15 min ==="
grep -ah "ERROR" bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep "2026-11-10 12:(4[0-9]|5[0-9])" | tail -10