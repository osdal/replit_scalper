#!/bin/bash
sleep 15
echo "=== env ==="
docker exec replit_scalper-api-live bash -lc 'env | grep -E "DAILY_LOSS|LIVE_DAILY|MAX_POSITIONS|DEPOSIT|BINANCE_TEST"'
echo "--- daily-loss endpoint ---"
curl -s http://localhost:5001/api/live/daily-loss 2>&1
echo ""
echo "--- recovery/check endpoint ---"
curl -s http://localhost:5001/api/recovery/check 2>&1
echo ""
echo "--- container status ---"
docker ps --filter name=replit_scalper-api-live --format '{{.Status}}'