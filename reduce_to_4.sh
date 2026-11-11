#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== Current running bots ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT symbol, is_running FROM bots ORDER BY symbol"):
    print(r)
PY
echo
echo "=== Setting is_running=1 for DOGE, LSK, PEPE, FIL; 0 for others ==="
python3 - <<'PY'
import sqlite3
keep = ['DOGEUSDT', 'LSKUSDT', '1000PEPEUSDT', 'FILUSDT']
con = sqlite3.connect('data/bot.db')
# First, stop all
con.execute("UPDATE bots SET is_running=0 WHERE is_running=1")
# Then enable only the 4
placeholders = ','.join(['?'] * len(keep))
con.execute(f"UPDATE bots SET is_running=1 WHERE symbol IN ({placeholders})", keep)
con.commit()
print("Updated.")
for r in con.execute("SELECT symbol, is_running FROM bots ORDER BY symbol"):
    print(r)
PY
echo
echo "=== Restarting api-testnet ==="
docker compose up -d --force-recreate api-testnet 2>&1 | tail -5
echo
sleep 10
echo "=== Processes after restart ==="
docker exec replit_scalper-api-testnet bash -lc 'ps aux | grep "[m]ain.py" | wc -l'
docker exec replit_scalper-api-testnet bash -lc 'ps aux | grep "[m]ain.py"'