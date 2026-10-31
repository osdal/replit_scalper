#!/bin/bash
echo "=== BOT_ENV / env контейнера ==="
docker exec replit_scalper-api-testnet sh -c 'env | sort | grep -Ei "BOT_ENV|LIVE|TESTNET|MARGIN|POSITION|REVERSE|MAX_POS|BINANCE" '
echo
echo "=== get_free_balance ==="
grep -n "async def get_free_balance" -A 60 /opt/trading-bot/bot/order_manager.py | head -80
echo
echo "=== cap нотионала (max_position_notional) ==="
grep -n "max_position_notional_usd" -B 5 -A 25 /opt/trading-bot/bot/order_manager.py | head -80
