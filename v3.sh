#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== containers ==="
docker ps --format '{{.Names}} {{.Status}}'
echo
echo "=== testnet env ==="
docker exec replit_scalper-api-testnet bash -lc 'env' 2>/dev/null | grep -E 'POST_BAN|SLOTS_FULL|MAX_POSITIONS=|STARTUP_STAGGER|BINANCE_WEIGHT_SOURCE' || echo "no match"
echo
echo "=== bot processes ==="
docker exec replit_scalper-api-testnet bash -lc 'ps aux | grep -c "[m]ain.py"' 2>&1
echo
echo "=== DB max_positions ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT symbol,max_positions,is_running FROM bots ORDER BY symbol"):
    print(r)
PY
echo
echo "=== API status ==="
curl -s -m 10 http://localhost:5000/api/trading/status | head -c 800
echo
