#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== .env.live reverse/loss keys ==="
grep -aE '^(REVERSE_[A-Z_]+|LIVE_DAILY_LOSS_LIMIT_[A-Z]+|LIVE_MAX_DRAWDOWN_PCT|DAILY_LOSS_LIMIT_PCT)=' .env.live | sort
echo
echo "=== testnet .env reverse keys ==="
grep -aE '^(REVERSE_[A-Z_]+|DAILY_LOSS_LIMIT_[A-Z]+)=' .env | sort || echo none
echo
echo "=== compose reverse env ==="
grep -n "REVERSE_" docker-compose.yml
