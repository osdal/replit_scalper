#!/usr/bin/env bash
set -euo pipefail

cd /opt/trading-bot
echo "=== Git fetch/reset ==="
git fetch origin main
git reset --hard FETCH_HEAD

echo "=== Recreate api-testnet ==="
docker compose up -d api-testnet

echo "=== Wait for API start ==="
sleep 5

echo "=== Check grid engine logs ==="
docker logs replit_scalper-api-testnet --since 30s | grep -i "grid engine" || true

echo "=== Done ==="