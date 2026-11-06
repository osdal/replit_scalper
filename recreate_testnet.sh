#!/bin/bash
cd /opt/trading-bot
docker compose up -d --force-recreate api-testnet 2>&1 | tail -5
sleep 25
echo "=== env ==="
docker exec replit_scalper-api-testnet bash -lc 'env' 2>&1 | grep -E "WEIGHT_SOURCE|POSITION_CACHE|SHED_AT|BINANCE_TESTNET"
echo "=== bots ==="
docker exec replit_scalper-api-testnet bash -lc 'ps -eo args' 2>&1 | grep -c "main.py"
echo "=== status ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'
echo "=== gate active ==="
grep -h "Gate ACTIVE" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -2
echo "=== errors 2m ==="
docker logs replit_scalper-api-testnet --since 2m 2>&1 | grep -iE "traceback|exception|critical" | grep -viE "GateBlocked|banned" | tail -5
