#!/bin/bash
docker exec replit_scalper-api-testnet ls -la /app/data/global_limits* 2>/dev/null || echo "not in container /app/data"
docker exec replit_scalper-api-testnet ls -la /app/bot/data/global_limits* 2>/dev/null || echo "not in /app/bot/data"
find /opt/trading-bot -name "global_limits*" 2>/dev/null || echo "not on host"