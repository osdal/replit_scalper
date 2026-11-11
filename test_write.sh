#!/bin/bash
echo "=== before write ==="
docker exec replit_scalper-api-testnet ls -la /app/data/global_limits* 2>/dev/null || echo "no file"
echo
echo "=== write via API ==="
docker exec replit_scalper-api-testnet curl -s -X POST http://localhost:5000/api/trading/global-limits -H 'Content-Type: application/json' -d '{"max_positions":7,"max_positions_with_expired":20}'
echo
echo "=== after write ==="
docker exec replit_scalper-api-testnet ls -la /app/data/global_limits* 2>/dev/null || echo "no file"
echo
echo "=== check /status ==="
docker exec replit_scalper-api-testnet curl -s http://localhost:5000/api/trading/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('max_positions:', d.get('max_positions'), 'max_positions_with_expired:', d.get('max_positions_with_expired'))"
echo
echo "=== read file content ==="
docker exec replit_scalper-api-testnet cat /app/data/global_limits_testnet.json 2>/dev/null || echo "no file"