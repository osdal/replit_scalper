#!/bin/bash
echo "=== live daily-loss ==="
curl -s http://localhost:5001/api/live/daily-loss 2>&1
echo ""
echo "=== trading/check ==="
curl -s -X POST http://localhost:5001/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"BTCUSDT"}' 2>&1
echo ""
echo "=== trading/status ==="
curl -s http://localhost:5001/api/trading/status 2>&1