#!/bin/bash
echo "=== full traceback (last one) ==="
awk '/Traceback/{buf=""} {buf=buf"\n"$0} END{print buf}' /opt/trading-bot/bot/logs/testnet/api_btcusdt_stdout.log | tail -40
echo ""
echo "=== trades table columns ==="
python3 -c "import sqlite3;c=sqlite3.connect('/opt/trading-bot/data/bot.db');print([r[1] for r in c.execute('PRAGMA table_info(trades)')])"
echo ""
echo "=== open trades ==="
python3 << 'EOF'
import sqlite3
c=sqlite3.connect("/opt/trading-bot/data/bot.db"); c.row_factory=sqlite3.Row
cols=[r[1] for r in c.execute("PRAGMA table_info(trades)")]
sel=[x for x in ("id","symbol","direction","status","entry_time","exit_time","reason") if x in cols]
rows=c.execute(f"select {','.join(sel)} from trades where is_open=1 order by id desc limit 20").fetchall()
print("open rows:", len(rows))
for r in rows: print(dict(r))
print("=== rows created after 12:30 ===")
rows=c.execute(f"select {','.join(sel)} from trades where entry_time > strftime('%s','2026-11-07 12:30:00') order by id desc limit 15").fetchall()
print("count:", len(rows))
for r in rows: print(dict(r))
EOF
echo ""
echo "=== how many bots still in STARTUP warm-up ==="
grep -l "warm-up klines failed" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | wc -l
echo "=== bots that reached listening ==="
grep -l "Listening for candles" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | wc -l
