#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect("data/bot.db"); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
recent = 0
for r in con.execute("SELECT symbol,last_heartbeat FROM bots WHERE is_running=1 AND armed=1"):
    try:
        et = datetime.datetime.fromisoformat(str(r["last_heartbeat"]).replace("Z","+00:00"))
        if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
        age = (now-et).total_seconds()
        if age < 300: recent += 1
    except: pass
print(f"recent (<5min): {recent}/40")
PY