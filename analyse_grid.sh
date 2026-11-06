#!/bin/bash
echo "=== grid-testnet container ==="
docker ps --filter name=trading-bot-dashboard-grid-testnet-1 --format '{{.Names}} | {{.Image}} | {{.Status}} | {{.Ports}}'
echo
echo "=== logs tail ==="
docker logs trading-bot-dashboard-grid-testnet-1 --tail 40 2>&1
echo
echo "=== log volume last 10m ==="
docker logs trading-bot-dashboard-grid-testnet-1 --since 10m 2>&1 | wc -l
docker logs trading-bot-dashboard-grid-testnet-1 --since 10m 2>&1 | tail -5
echo
echo "=== env (masked) ==="
docker exec -i trading-bot-dashboard-grid-testnet-1 sh -lc 'env | grep -iE "BINANCE|KEY|SECRET|SYMBOL|GRID" | sed "s/=.*/=<set>/"' 2>&1 | head -30
echo
echo "=== key hash if present ==="
docker exec -i trading-bot-dashboard-grid-testnet-1 python3 - <<'PY' 2>&1
import os, hashlib
k=os.getenv("BINANCE_API_KEY","")
print("key hash:", hashlib.sha1(k.encode()).hexdigest()[:8] if k else "NONE", "len:", len(k))
print("testnet:", os.getenv("BINANCE_TESTNET"), "env:", os.getenv("BOT_ENV"))
PY
echo
echo "=== compose labels for this container ==="
docker inspect trading-bot-dashboard-grid-testnet-1 --format '{{json .Config.Labels}}' 2>&1 | head -c 1200
echo
