#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== UTC now ==="
date -u
echo
echo "=== ban files / gate state ==="
ls -la data/*ban* logs/*ban* 2>/dev/null || echo "(no ban files at this level)"
echo
echo "=== last ban mentions in bot stdout ==="
grep -ahE "banned until" bot/logs/testnet/api_*usdt_stdout.log 2>/dev/null | tail -3 | sed 's/\x1b\[[0-9;]*m//g'
echo
echo "=== recent gate/ban lines (last 3 min) ==="
tail -n 400 bot/logs/testnet/api_dogeusdt_stdout.log 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g' | grep -aE "\[GATE\]|\[LEVERAGE\]|banned|RATE_LIMIT|SLOTS|ENTRY|POSITION" | tail -12
echo
echo "=== positions (testnet) ==="
curl -s http://localhost:5000/api/trading/positions | head -c 800
echo
echo "=== bots uptime ==="
docker exec replit_scalper-api-testnet sh -lc 'ps -eo pid,etime,cmd | grep -a "main.py" | grep -v grep'
echo
echo "=== baseline files ==="
ls -la data/daily_baseline*.json data/live_daily_loss_state*.json 2>/dev/null
for f in data/daily_baseline_*.json data/live_daily_loss_state_*.json; do [ -f "$f" ] && { echo "--- $f"; cat "$f"; echo; }; done
