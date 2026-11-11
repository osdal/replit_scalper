#!/bin/bash
# Запускаем несколько тестовых ботов через API
for sym in BTCUSDT ETHUSDT BNBUSDT SOLUSDT XRPUSDT; do
  echo "Starting $sym..."
  curl -s -X POST "http://localhost:5000/api/bots/$sym/start" | head -c 200
  echo ""
  sleep 1
done
echo "=== Bot statuses ==="
curl -s "http://localhost:5000/api/bots" | jq '.[] | {symbol, is_running, stop_requested}' 2>/dev/null || curl -s "http://localhost:5000/api/bots"