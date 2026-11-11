#!/bin/bash
echo "=== live bots ==="
docker exec replit_scalper-api-live bash -lc 'ps -eo args' 2>&1 | grep -c "main.py"
echo "=== testnet bots ==="
docker exec replit_scalper-api-testnet bash -lc 'ps -eo args' 2>&1 | grep -c "main.py"
echo ""
echo "=== live daily-loss (should show limit ~0.95) ==="
curl -s http://localhost:5001/api/live/daily-loss 2>&1
echo ""
echo "=== trading/check (max_positions should be 1) ==="
curl -s -X POST http://localhost:5001/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"BTCUSDT"}' 2>&1
echo ""
echo "=== errors in live api ==="
docker logs replit_scalper-api-live --since 5m 2>&1 | grep -iE "error|traceback|exception" | grep -v "cannot GET\|404" | tail -10