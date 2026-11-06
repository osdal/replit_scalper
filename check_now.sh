#!/bin/bash
cd /opt/trading-bot
date -u
python3 -c "import datetime; ts=1793870159533/1000; print(datetime.datetime.fromtimestamp(ts, datetime.timezone.utc))"
echo "=== gate config ==="
docker compose config 2>/dev/null | grep -A 5 "api-testnet:" | grep -E "BINANCE_|REVERSE_"
echo "=== bot heartbeat ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
for r in con.execute("SELECT symbol,last_heartbeat FROM bots WHERE is_running=1 AND armed=1"):
    try:
        et = datetime.datetime.fromisoformat(str(r['last_heartbeat']).replace('Z','+00:00'))
        if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
        age = (now-et).total_seconds()
        print(f"{r['symbol']:<12} {age:.0f}s")
    except:
        pass
PY