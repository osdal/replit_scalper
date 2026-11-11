#!/bin/bash
date -u
echo "=== processes (pid, start, cmd) ==="
docker exec replit_scalper-api-testnet bash -lc 'ps -eo pid,etime,cmd | grep "[m]ain.py"' 2>&1
echo
echo "=== restarts since 11:40 (count of STARTUP staggered per log) ==="
grep -ah "STARTUP] staggered delay" /opt/trading-bot/bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep -c "2026-11-10 11:" 
echo
echo "=== last 12 lines: pepe / ada / doge ==="
for s in 1000pepeusdt adausdt dogeusdt; do
  echo "--- $s"
  tail -6 /opt/trading-bot/bot/logs/testnet/api_${s}_stdout.log 2>/dev/null
done
echo
echo "=== any real network ban responses since 11:47 (not gate blocked) ==="
grep -ah "code=-1003" /opt/trading-bot/bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep -v "gate blocked" | tail -5 || echo none
