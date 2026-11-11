#!/bin/bash
echo "=== write via API (host curl) ==="
curl -s -X POST http://localhost:5000/api/trading/global-limits -H 'Content-Type: application/json' -d '{"max_positions":7,"max_positions_with_expired":20}'
echo
echo "=== check file on host ==="
ls -la /opt/trading-bot/data/global_limits* 2>/dev/null || echo "no file on host"
echo
echo "=== check /status ==="
curl -s http://localhost:5000/api/trading/status > /tmp/status.json
python3 -c "import sys,json; d=json.load(open('/tmp/status.json')); print('max_positions:', d.get('max_positions'), 'max_positions_with_expired:', d.get('max_positions_with_expired'))"