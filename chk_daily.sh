#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
start = datetime.datetime.utcnow().strftime('%Y-%m-%d')
cur = con.execute("SELECT exit_time, pnl, symbol, status, is_open FROM trades WHERE is_open=0 AND pnl<0 AND exit_time >= ? ORDER BY exit_time", (start,))
for r in cur.fetchall(): print(r)
PY