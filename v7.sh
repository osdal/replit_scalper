#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
sudo python3 - <<'PY'
import sqlite3
syms = ['ADAUSDT','FILUSDT','LSKUSDT','NEARUSDT','1000PEPEUSDT','TRXUSDT','DOGEUSDT','INJUSDT']
con = sqlite3.connect('data/bot.db')
con.execute("UPDATE bots SET max_positions=3 WHERE symbol IN (%s)" % ",".join("?"*len(syms)), syms)
con.commit()
print("updated rows:", con.total_changes)
for r in con.execute("SELECT symbol,max_positions,is_running FROM bots WHERE is_running=1 ORDER BY symbol"):
    print(r)
PY
echo
echo "=== status ==="
curl -s -m 10 http://localhost:5000/api/trading/status
echo
echo "=== live bots check (must be 0) ==="
docker exec replit_scalper-api-live bash -lc 'ps aux | grep -c "[m]ain.py"' 2>&1
