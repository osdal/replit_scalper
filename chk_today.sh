#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
start = datetime.datetime.utcnow().strftime('%Y-%m-%d')
cur = con.execute("SELECT COALESCE(SUM(-pnl),0), COUNT(*) FROM trades WHERE is_open=0 AND pnl<0 AND exit_time >= ?", (start,))
print('today_utc', start, cur.fetchone())
cur = con.execute("SELECT COUNT(*), SUM(is_open) FROM trades WHERE status != 'rejected' AND is_open=1")
print('open accepted', cur.fetchone())
cur = con.execute("SELECT symbol, ROUND((julianday('now')-julianday(entry_time))*24,1) FROM trades WHERE status != 'rejected' AND is_open=1 ORDER BY entry_time")
for r in cur.fetchall(): print(r)
PY
