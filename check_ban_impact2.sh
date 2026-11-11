#!/bin/bash
echo "=== open positions (DB) ==="
python3 << 'EOF'
import sqlite3
c=sqlite3.connect("/opt/trading-bot/data/bot.db"); c.row_factory=sqlite3.Row
rows=c.execute("select id,symbol,direction,entry_price,remaining_qty,entry_time,status from trades where is_open=1 order by id desc limit 20").fetchall()
print("open rows:", len(rows))
for r in rows: print(dict(r))
print("")
print("=== rows created since 12:30 UTC ===")
rows=c.execute("select id,symbol,status,reason,entry_time,exit_time from trades where entry_time > strftime('%s','2026-11-07 12:30:00') order by id desc limit 15").fetchall()
print("count:", len(rows))
for r in rows: print(dict(r))
print("")
print("=== last 10 rows regardless ===")
for r in c.execute("select id,symbol,status,reason,datetime(entry_time,'unixepoch') as et from trades order by id desc limit 10"):
    print(dict(r))
EOF
echo ""
echo "=== ensure protection traceback (last full) ==="
grep -h -A12 "Traceback" /opt/trading-bot/bot/logs/testnet/api_btcusdt_stdout.log 2>/dev/null | tail -30
echo ""
echo "=== errors in last 200 lines of BTC stdout ==="
grep -E "ERROR|Traceback|failed" /opt/trading-bot/bot/logs/testnet/api_btcusdt_stdout.log 2>/dev/null | tail -10
