#!/bin/bash
set -u
cd /opt/trading-bot || exit 1
echo "=== git pull ==="
git pull --ff-only origin main 2>&1 | tail -6
echo
echo "=== recreate api-testnet api-live ==="
docker compose up -d --force-recreate api-testnet api-live 2>&1 | tail -8
sleep 15
echo
echo "=== containers ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'api-testnet|api-live'
echo
echo "=== testnet /status (ban field) ==="
curl -s http://localhost:5000/api/trading/status | head -c 600
echo
echo "=== live /status (ban field) ==="
curl -s http://localhost:5001/api/trading/status | head -c 600
echo
echo "=== gate files (newest) ==="
for f in $(ls -t /opt/trading-bot/bot/data/binance_gate_testnet.json* 2>/dev/null | head -3); do echo "--- $f"; cat "$f"; echo; done
echo
echo "=== testnet api logs (ban messages) ==="
docker logs --tail 100 replit_scalper-api-testnet 2>&1 | grep -aiE "ban active|skipped to avoid|Binance ban" | tail -5