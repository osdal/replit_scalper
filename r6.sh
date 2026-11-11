#!/bin/bash
date -u
echo "=== testnet env risk ==="
docker exec replit_scalper-api-testnet bash -lc 'env' 2>&1 | grep -E 'MAX_POSITIONS|DAILY_LOSS|LOSS_STREAK|POSITION_SIZE|KILL|RISK' || true
echo
echo "=== testnet yaml max_positions (each config) ==="
docker exec replit_scalper-api-testnet bash -lc "grep -h 'max_positions' /app/bot/configs/testnet/config_ada.yaml /app/bot/configs/testnet/config_lsk.yaml /app/bot/configs/testnet/config_fil.yaml 2>/dev/null" 2>&1
echo
echo "=== no cached balance context (sample) ==="
grep -h "no cached balance available" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | tail -3
grep -h "position_size_pct set but free balance unavailable" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | tail -3
echo
echo "=== balance endpoint check (from host) ==="
curl -s -m 10 http://localhost:5000/api/account/balance | head -c 400
echo
echo "=== risk endpoint check ==="
curl -s -m 10 http://localhost:5000/api/risk/check | head -c 400
echo
