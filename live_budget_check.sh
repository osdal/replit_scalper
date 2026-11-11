#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== live wallet / drawdown (api-live, read-only) ==="
curl -s -m 15 http://localhost:5001/api/live/drawdown
echo
curl -s -m 15 http://localhost:5001/api/live/daily-loss
echo
echo "=== live bots is_running ==="
sudo python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot_live.db')
rows = list(con.execute("SELECT symbol,is_running,max_positions FROM bots WHERE is_running=1"))
print("running:", rows if rows else "none")
print("total rows:", list(con.execute("SELECT COUNT(*) FROM bots"))[0][0])
PY
echo
echo "=== live bot processes (must be 0) ==="
docker exec replit_scalper-api-live bash -lc 'ps -eo pid,cmd | grep "[m]ain.py" | wc -l'
