#!/bin/bash
cd /opt/trading-bot
echo "=== api-live block (env) ==="
sed -n '/^  api-live:/,/^  dashboard-live:/p' docker-compose.yml | grep -E "LOSS_STREAK_TRIGGER|MAX_POSITIONS|BOT_ENV|PORT="
echo
echo "=== api-testnet block (env) ==="
sed -n '/^  api-testnet:/,/^  dashboard-testnet:/p' docker-compose.yml | grep -E "LOSS_STREAK_TRIGGER|MAX_POSITIONS|BOT_ENV|PORT="
