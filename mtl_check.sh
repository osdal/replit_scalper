#!/bin/bash
cd /opt/trading-bot
echo "=== env files ==="
ls -a | grep -i env
echo
echo "=== MTL строки ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT id,symbol,direction,entry_time,exit_time,is_open,status,pnl,qty,entry_price,exit_reason FROM trades WHERE symbol='MTLUSDT' ORDER BY id DESC LIMIT 5"):
    print(r)
print('now utc:', con.execute("SELECT datetime('now')").fetchone()[0])
PY
echo
echo "=== MTL лог последние строки ==="
tail -25 bot/logs/testnet/api_mtlusdt_stdout.log 2>/dev/null
