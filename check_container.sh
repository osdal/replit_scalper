#!/bin/bash
cd /opt/trading-bot
echo "=== check code in container ==="
docker exec replit_scalper-api-testnet bash -lc 'grep -n "staggered delay" /app/bot/main.py | head -2'
echo "=== check SHED_AT in container env ==="
docker exec replit_scalper-api-testnet bash -lc 'echo $BINANCE_SHED_AT'
echo "=== recent logs for ETH (startup) ==="
grep -h "STARTUP\|staggered" bot/logs/testnet/api_ethusdt_stdout.log 2>/dev/null | tail -10