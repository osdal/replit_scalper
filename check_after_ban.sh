#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== UTC now ==="
date -u
echo
echo "=== ban status ==="
curl -s http://localhost:5000/api/trading/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('testnet ban_until_ms:', d.get('binance_ban_until_ms', 0))"
echo
echo "=== bot processes ==="
docker exec replit_scalper-api-testnet sh -lc 'ps -eo pid,etime,cmd | grep -a "main.py" | grep -v grep'
echo
echo "=== testnet api logs (ban/startup/leverage) ==="
docker logs --tail 80 replit_scalper-api-testnet 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -aE "ban active|skipped|SYNC|LEVERAGE|warm-up|started|exited|ENTRY|position|set 75x" | tail -20
echo
echo "=== bot stdout (doge, last 15) ==="
tail -n 15 bot/logs/testnet/api_dogeusdt_stdout.log 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g'
echo
echo "=== bot stdout (pepe, last 10) ==="
tail -n 10 bot/logs/testnet/api_1000pepeusdt_stdout.log 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g'