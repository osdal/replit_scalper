#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("data/bot.db"); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,symbol,direction,qty,entry_price,pnl,entry_time,exit_time,is_open,exit_reason
FROM trades WHERE symbol="ETHUSDT" ORDER BY entry_time"""):
    print(dict(r))
PY