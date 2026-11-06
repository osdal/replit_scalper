#!/bin/bash
cd /opt/trading-bot
docker compose config 2>/dev/null | awk '/^  api-testnet:/,/^  [a-z-]+:/' | grep -E "BINANCE_SHED|BINANCE_WEIGHT|BINANCE_GATE"