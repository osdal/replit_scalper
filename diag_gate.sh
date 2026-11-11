#!/bin/bash
echo "=== gate env in container ==="
docker exec replit_scalper-api-testnet bash -lc 'env | grep -E "BINANCE_WEIGHT|BINANCE_SHED|BINANCE_GATE|POSITION_CACHE"'
echo ""
echo "=== demand lines last 10 min ==="
grep -h "RATE_LIMIT] demand" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -8
echo ""
echo "=== gate file sample (fresh) ==="
cd /opt/trading-bot/bot/data
for f in $(ls -t binance_gate_testnet.json.* 2>/dev/null | head -3); do echo -n "$f: "; cat "$f"; echo; done
echo ""
echo "=== stats lines with local/effective ==="
grep -h "RATE_LIMIT] used_weight" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -5
echo ""
echo "=== ban lines 09:55-10:05 ==="
grep -h -E "^2026-11-06 09:(5[5-9]):[0-9]{2}.*IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | head -5