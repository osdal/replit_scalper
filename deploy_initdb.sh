#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== deploy ==="
git fetch origin main && git reset --hard FETCH_HEAD
echo
echo "=== testnet env MAX_POSITIONS ==="
docker exec replit_scalper-api-testnet bash -lc 'env | grep -E "^MAX_POSITIONS=|^SLOTS_FULL_SOFT_PAUSE="'
echo
echo "=== run init-db manually (no container restart, bots untouched) ==="
docker exec replit_scalper-api-testnet bash -lc 'pnpm run init-db 2>&1 | tail -8'
echo
echo "=== bots.max_positions after init-db ==="
sudo python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT symbol,max_positions,is_running FROM bots WHERE is_running=1 ORDER BY symbol"):
    print(r)
PY
echo
echo "=== bot processes still alive ==="
docker exec replit_scalper-api-testnet bash -lc 'ps -eo pid,etime,cmd | grep "[m]ain.py"'
