#!/bin/bash
echo "=== test endpoint ==="
curl -s -X POST http://localhost:5000/api/trading/global-limits -H 'Content-Type: application/json' -d '{"max_positions":9,"max_positions_with_expired":25}'
echo
echo "=== check files ==="
ls -la /opt/trading-bot/data/global_limits* 2>/dev/null || echo "no file on host"
docker exec replit_scalper-api-testnet ls -la /app/data/global_limits* 2>/dev/null || echo "no file in container"
echo
echo "=== API logs ==="
docker logs --tail 30 replit_scalper-api-testnet 2>&1 | sed 's/\x1b\[[0-9;]*m//g' | grep -E 'global-limits|writeGlobalLimits|readGlobalLimits|FILE PATH|WRITE OK|ERROR|already been declared'