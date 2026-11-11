#!/bin/bash
set -u
cd /opt/trading-bot || exit 1
echo "=== git pull ==="
git pull --ff-only origin main 2>&1 | tail -6
echo
echo "=== recreate both API containers ==="
docker compose up -d --force-recreate api-testnet api-live 2>&1 | tail -8
sleep 10
echo
echo "=== testnet /status ==="
curl -s http://localhost:5000/api/trading/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('max_positions:', d.get('max_positions'), 'max_positions_with_expired:', d.get('max_positions_with_expired'))"
echo
echo "=== live /status ==="
curl -s http://localhost:5001/api/trading/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('max_positions:', d.get('max_positions'), 'max_positions_with_expired:', d.get('max_positions_with_expired'))"
echo
echo "=== test global-limits endpoint (testnet) ==="
curl -s -X POST http://localhost:5000/api/trading/global-limits -H 'Content-Type: application/json' -d '{"max_positions":5,"max_positions_with_expired":15}' | head -c 200
echo
echo "=== testnet /status after ==="
curl -s http://localhost:5000/api/trading/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('max_positions:', d.get('max_positions'), 'max_positions_with_expired:', d.get('max_positions_with_expired'))"
echo
echo "=== check file ==="
cat /opt/trading-bot/data/global_limits_testnet.json 2>/dev/null || echo "no file"