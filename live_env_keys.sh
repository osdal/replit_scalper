#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== .env.live risk keys (values only for non-secret knobs) ==="
grep -aE '^(MAX_POSITIONS|DAILY_LOSS_LIMIT_PCT|DAILY_LOSS_LIMIT_USD|POST_BAN_COOLDOWN_SEC|POST_BAN_JITTER_SEC|BINANCE_WEIGHT_SOURCE|BINANCE_WEIGHT_LIMIT_1M|BINANCE_SHED_AT|LOSS_STREAK_TRIGGER|MAX_POSITIONS_IGNORE_AFTER_HOURS|SLOTS_FULL_SOFT_PAUSE|STARTUP_STAGGER_MAX_SEC|DEPOSIT_USD|EXPIRED_HARD_CLOSE_HOURS|KILL_SWITCH|DRAWDOWN)' .env.live || echo "no such keys"
echo
echo "=== .env (testnet) same keys ==="
grep -aE '^(MAX_POSITIONS|DAILY_LOSS_LIMIT_PCT|DAILY_LOSS_LIMIT_USD|BINANCE_WEIGHT_SOURCE|SLOTS_FULL_SOFT_PAUSE)=' .env || echo "no such keys"
echo
echo "=== compose live API env for daily loss ==="
grep -n "DAILY_LOSS" docker-compose.yml
