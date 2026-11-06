#!/bin/bash
cd /opt/trading-bot
echo "=== binance gate config в compose ==="
docker compose config 2>/dev/null | grep -A 5 "api-testnet:" | grep -E "BINANCE_|REVERSE_"
echo
echo "=== binance_gate.py: критические запросы ==="
grep -n "is_critical\|CRITICAL" bot/binance_gate.py | head -15
echo
echo "=== последние ошибки перед баном ==="
grep -h -B2 "banned until" bot/logs/testnet/api_*.log 2>/dev/null | head -30