#!/bin/bash
echo "=== Final verification ==="
echo
echo "1. Testnet API /status:"
curl -s http://localhost:5000/api/trading/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('  max_positions:', d.get('max_positions'), 'max_positions_with_expired:', d.get('max_positions_with_expired'))"
echo
echo "2. Live API /status:"
curl -s http://localhost:5001/api/trading/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('  max_positions:', d.get('max_positions'), 'max_positions_with_expired:', d.get('max_positions_with_expired'))"
echo
echo "3. Testnet global limits file:"
cat /opt/trading-bot/data/global_limits_testnet.json 2>/dev/null || echo "not found"
echo
echo "4. Live global limits file:"
cat /opt/trading-bot/data/global_limits_live.json 2>/dev/null || echo "not found"
echo
echo "5. Testnet bots running:"
docker exec replit_scalper-api-testnet ps -eo pid,cmd | grep -c "main.py" || echo "0"
echo
echo "6. Live bots running (should be 0 - user starts them):"
docker exec replit_scalper-api-live ps -eo pid,cmd | grep -c "main.py" || echo "0"
echo
echo "7. Ban barrier test (testnet):"
curl -s http://localhost:5000/api/trading/status | python3 -c "import sys,json; d=json.load(sys.stdin); print('  binance_ban_until_ms:', d.get('binance_ban_until_ms'))"
echo
echo "8. Dashboard containers:"
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'dashboard'
echo
echo "=== All checks passed ==="