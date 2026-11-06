#!/bin/bash
cd /opt/trading-bot
echo "=== ban lines hour 05 (symbols) ==="
grep -h "IP banned" bot/logs/testnet/api_*.log 2>/dev/null | grep "2026-11-06 05" | sed -E 's/.*\[LIVE ([A-Z0-9]+)\].*(consecutive=[0-9]+).*/\1 \2/' | sort | uniq -c
echo
echo "=== last 5 ban lines with symbol ==="
grep -h "IP banned" bot/logs/testnet/api_*.log 2>/dev/null | tail -5
echo
echo "=== routes with balance/equity ==="
grep -rhoE 'router\.(get|post)\("/(balance|equity|account|summary)[^"]*"' artifacts/api-server/dist/routes/*.js 2>/dev/null | sort -u | head -20
echo
for ep in binance-sync/balance binance-sync/account trading/balance live/account live/status live/summary; do
  echo "--- /api/$ep ---"
  curl -s "http://localhost:5000/api/$ep" 2>/dev/null | head -c 600
  echo
done
