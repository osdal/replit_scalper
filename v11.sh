#!/bin/bash
date -u
echo "=== inside container: find gate files ==="
docker exec replit_scalper-api-testnet bash -lc 'ls -la /app/bot/data/ 2>/dev/null | head; echo ---; find /app -name "binance_gate*" -maxdepth 4 2>/dev/null' 2>&1
echo
echo "=== mounts ==="
docker inspect replit_scalper-api-testnet --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{println}}{{end}}'
