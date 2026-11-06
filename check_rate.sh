#!/bin/bash
cd /opt/trading-bot
echo "=== последние ошибки -1003 в логах ==="
grep -h "\-1003\|rate.limit\|banned\|weight" bot/logs/testnet/api_*.log 2>/dev/null | grep "2026-11-05 0[89]:" | tail -10
echo
echo "=== последняя активность ботов ==="
grep -h "TICK_SL_TP\|process_hit\|EXPIRED" bot/logs/testnet/api_ethusdt_stdout.log 2>/dev/null | tail -5
echo
echo "=== проверка rate gate status ==="
curl -s http://localhost:5000/api/binance/gate/status 2>/dev/null | python3 -m json.tool 2>/dev/null || echo "endpoint не найден"