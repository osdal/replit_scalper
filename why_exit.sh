#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== stderr log doge (tail 40) ==="
tail -n 40 bot/logs/testnet/api_dogeusdt.log 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g'
echo
echo "=== stdout doge: last 30 lines ==="
tail -n 30 bot/logs/testnet/api_dogeusdt_stdout.log 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g'
echo
echo "=== all log files for doge (which is stderr?) ==="
ls -la bot/logs/testnet/ | grep -i doge
echo
echo "=== process state now ==="
docker exec replit_scalper-api-testnet sh -lc 'ps -eo pid,etime,stat,cmd | grep -a "main.py" | grep -v grep'
echo
echo "=== last api-log lines ==="
docker logs --tail 25 replit_scalper-api-testnet 2>&1 | sed 's/\x1b\[[0-9;]*m//g'
