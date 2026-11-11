#!/bin/bash
set -u
cd /opt/trading-bot || exit 1
echo "=== git pull ==="
git pull --ff-only origin main 2>&1 | tail -6
echo
echo "=== recreate both API containers ==="
docker compose up -d --force-recreate api-testnet api-live 2>&1 | tail -8
sleep 15
echo
echo "=== containers ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -E 'api-testnet|api-live'
echo
echo "=== testnet /status ==="
curl -s http://localhost:5000/api/trading/status | head -c 600
echo
echo "=== live /status ==="
curl -s http://localhost:5001/api/trading/status | head -c 600
echo
echo "=== testnet DB risk cols ==="
python3 - <<'PY'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db')
cols = [r[1] for r in c.execute("PRAGMA table_info(bots)")]
print("max_positions:", "max_positions" in cols)
print("max_positions_with_expired:", "max_positions_with_expired" in cols)
PY
echo
echo "=== live DB risk cols ==="
python3 - <<'PY'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot_live.db')
cols = [r[1] for r in c.execute("PRAGMA table_info(bots)")]
print("max_positions:", "max_positions" in cols)
print("max_positions_with_expired:", "max_positions_with_expired" in cols)
PY