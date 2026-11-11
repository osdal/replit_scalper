#!/bin/bash
echo "=== trading/check endpoint ==="
curl -s -X POST http://localhost:5001/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"BTCUSDT"}' 2>&1
echo ""
echo "=== trading/status endpoint ==="
curl -s http://localhost:5001/api/trading/status 2>&1
echo ""
echo "=== recovery/config endpoint ==="
curl -s http://localhost:5001/api/recovery/config 2>&1