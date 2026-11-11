#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== build api-live ==="
docker compose build api-live 2>&1 | tail -10
echo "=== recreate api-live ==="
docker compose up -d --no-deps api-live 2>&1 | tail -5
sleep 5
echo "=== live env ==="
docker exec replit_scalper-api-live bash -lc 'env | grep -E "POST_BAN|SLOTS_FULL|MAX_POSITIONS="'
echo "=== live bot processes (must be 0) ==="
docker exec replit_scalper-api-live bash -lc 'ps aux | grep -c "[m]ain.py"'