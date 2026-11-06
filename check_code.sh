#!/bin/bash
cd /opt/trading-bot
echo "=== проверка EXPIRED_HARD_CLOSE_HOURS в коде ==="
docker exec replit_scalper-api-testnet bash -lc 'grep -n "EXPIRED_HARD_CLOSE_HOURS" /app/bot/main.py | head -5'
echo
echo "=== проверка deadline в CapSnapshot ==="
docker exec replit_scalper-api-testnet bash -lc 'grep -n "deadline" /app/bot/cum_loss_cap.py | head -10'
echo
echo "=== проверка REVERSE_EXPIRED_DEADLINE ==="
docker exec replit_scalper-api-testnet bash -lc 'grep -n "REVERSE_EXPIRED_DEADLINE\|EXPIRED_DEADLINE" /app/bot/main.py | head -10'