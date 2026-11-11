#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== git pull ==="
git pull --ff-only origin main 2>&1 | tail -4
echo
echo "=== recreate api-testnet ==="
docker compose up -d --force-recreate api-testnet 2>&1 | tail -6
sleep 8
echo
echo "=== test endpoint ==="
curl -s -X POST http://localhost:5000/api/trading/global-limits -H 'Content-Type: application/json' -d '{"max_positions":9,"max_positions_with_expired":25}'
echo
echo "=== check files ==="
ls -la /opt/trading-bot/data/global_limits* 2>/dev/null || echo "no file on host"
docker exec replit_scalper-api-testnet ls -la /app/data/global_limits* 2>/dev/null || echo "no file in container"
echo
echo "=== check /status ==="
curl -s http://localhost:5000/api/trading/status > /tmp/status.json
python3 -c "import sys,json; d=json.load(open('/tmp/status.json')); print('max_positions:', d.get('max_positions'), 'max_positions_with_expired:', d.get('max_positions_with_expired'))"
echo
echo "=== API logs (last 20) ==="
docker logs --tail 20 replit_scalper-api-testnet 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -E 'global-limits|writeGlobalLimits|readGlobalLimits|FILE PATH|WRITE OK|ERROR'