#!/bin/bash
set -u
cd /opt/trading-bot || exit 1
echo "=== git status ==="
git status --short | head -20
echo
echo "=== git reset hard ==="
git reset --hard HEAD
echo
echo "=== git pull ==="
git pull --ff-only origin main 2>&1 | tail -6
echo
echo "=== recreate api-live ==="
docker compose up -d --force-recreate api-live 2>&1 | tail -6
sleep 10
echo
echo "=== live DB after re-seed ==="
python3 - <<'PY'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot_live.db')
cols = [r[1] for r in c.execute("PRAGMA table_info(bots)")]
print("new cols:", [x for x in ("slot_release_minutes","position_loss_pct","total_loss_pct","max_positions","max_positions_with_expired") if x in cols])
for row in c.execute("SELECT symbol,leverage,reverse_chain_max,max_positions,max_positions_with_expired,slot_release_minutes,position_loss_pct,total_loss_pct,is_running FROM bots WHERE symbol IN ('DOGEUSDT','LSKUSDT','1000PEPEUSDT','FILUSDT') ORDER BY symbol"):
    print(row)
PY