#!/bin/bash
echo "=== bans after 13:47 UTC ==="
grep -h "Way too many requests" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null \
  | grep -E "^2026-11-07 1[4-9]:|^2026-11-07 13:(4[7-9]|5[0-9])" | wc -l
echo "=== last 3 real ban lines (any time) ==="
grep -h "Way too many requests" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | tail -3 | cut -c1-100
echo "=== DB running bots ==="
python3 -c "import sqlite3;c=sqlite3.connect('/opt/trading-bot/data/bot.db');r=[x[0] for x in c.execute('select symbol from bots where is_running=1 order by symbol')];print(len(r), r)"
echo "=== open positions ==="
python3 -c "import sqlite3;c=sqlite3.connect('/opt/trading-bot/data/bot.db');print(c.execute('select symbol, direction, round(remaining_qty,4), datetime(entry_time) from trades where is_open=1').fetchall())"
