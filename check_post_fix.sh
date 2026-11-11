#!/bin/bash
cd /opt/trading-bot || exit 1
sleep 30
echo "=== processes ==="
docker exec replit_scalper-api-testnet bash -lc 'ps aux | grep -c "[m]ain.py"'
echo
echo "=== free balance errors ==="
grep -ah "free balance unavailable" bot/logs/testnet/api_*_stdout.log 2>/dev/null | tail -5 || echo "no free balance errors"
echo
echo "=== signals ==="
grep -ah "New signal received" bot/logs/testnet/api_*_stdout.log 2>/dev/null | tail -5 || echo "no signals yet"
echo
echo "=== trades after restart ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT id,symbol,direction,ROUND(entry_price,6),status,entry_time FROM trades WHERE entry_time >= datetime('now','-1 hour') AND status!='rejected' ORDER BY id DESC LIMIT 10"):
    print(r)
print("open:", list(con.execute("SELECT symbol,direction FROM trades WHERE is_open=1")))
PY