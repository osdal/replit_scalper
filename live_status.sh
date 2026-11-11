#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== live trading status (effective config) ==="
curl -s -m 15 http://localhost:5001/api/trading/status
echo
echo
echo "=== env.ts override logic ==="
sed -n '20,40p' artifacts/api-server/src/env.ts
echo
echo "=== .env.live full key list (names only) ==="
grep -aoE '^[A-Z0-9_]+' .env.live | sort
