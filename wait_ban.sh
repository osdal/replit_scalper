#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== UTC now ==="
date -u
echo
echo "=== ban status ==="
curl -s http://localhost:5000/api/trading/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('testnet ban_until_ms:', d.get('binance_ban_until_ms', 0))"
curl -s http://localhost:5001/api/trading/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('live ban_until_ms:', d.get('binance_ban_until_ms', 0))"
echo
echo "=== bot processes ==="
docker exec replit_scalper-api-testnet sh -lc 'ps -eo pid,etime,cmd | grep -a "main.py" | grep -v grep'
echo
echo "=== testnet api logs (last 30, ban/startup) ==="
docker logs --tail 50 replit_scalper-api-testnet 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -aE "ban active|skipped|SYNC|LEVERAGE|warm-up|started|exited|ENTRY|position" | tail -15
echo
echo "=== bot stdout (doge, tail 10) ==="
tail -n 10 bot/logs/testnet/api_dogeusdt_stdout.log 2>/dev/null | sed 's/\x1b\[[0-9;]*m//g'