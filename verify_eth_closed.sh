#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("data/bot.db"); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,symbol,exit_reason,exit_time FROM trades WHERE symbol='ETHUSDT' AND exit_reason='INFRA_BAN_DELAY'"""):
    print(dict(r))
PY