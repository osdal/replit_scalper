#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== recreate api-live ==="
docker compose up -d --force-recreate api-live 2>&1 | tail -6
sleep 8
echo
echo "=== test live endpoint ==="
curl -s -X POST http://localhost:5001/api/trading/global-limits -H 'Content-Type: application/json' -d '{"max_positions":5,"max_positions_with_expired":12}'
echo
echo "=== check files ==="
ls -la /opt/trading-bot/data/global_limits* 2>/dev/null
echo
echo "=== check live /status ==="
curl -s http://localhost:5001/api/trading/status > /tmp/status.json
python3 -c "import sys,json; d=json.load(open('/tmp/status.json')); print('max_positions:', d.get('max_positions'), 'max_positions_with_expired:', d.get('max_positions_with_expired'))"