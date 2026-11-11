#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== recreate dashboard containers ==="
docker compose up -d --force-recreate dashboard-testnet dashboard-live 2>&1 | tail -8
sleep 10
echo
echo "=== containers ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'dashboard'
echo
echo "=== testnet dashboard ==="
curl -s http://localhost:5173/ | grep -o 'Глобальные лимиты' || echo "not found"
echo
echo "=== live dashboard ==="
curl -s http://localhost:5176/ | grep -o 'Глобальные лимиты' || echo "not found"