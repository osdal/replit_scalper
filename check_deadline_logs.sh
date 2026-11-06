#!/bin/bash
cd /opt/trading-bot
echo "=== проверка логов на EXPIRED_DEADLINE ==="
grep -h "EXPIRED_DEADLINE" bot/logs/testnet/api_*.log 2>/dev/null | tail -5
echo
echo "=== проверка логов на EXPIRED_PROFIT (текущий день) ==="
grep -h "EXPIRED_PROFIT" bot/logs/testnet/api_*.log 2>/dev/null | grep "2026-11-05" | tail -5
echo
echo "=== боты: есть ли ошибки EXPIRED ==="
grep -h "EXPIRED" bot/logs/testnet/api_*.log 2>/dev/null | grep "2026-11-05 09:" | tail -3