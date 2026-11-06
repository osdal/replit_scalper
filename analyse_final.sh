#!/bin/bash
cd /opt/trading-bot
echo "=== TON price sanity ==="
python3 - <<'PY'
import sqlite3
con=sqlite3.connect("data/bot.db"); con.row_factory=sqlite3.Row
for s in ("TONUSDT","TAOUSDT","WLDUSDT","ETHUSDT"):
    r=con.execute("SELECT symbol,current_price,updated_at FROM bots WHERE symbol=?", (s,)).fetchone()
    print(f"{s:<10} bot_current_price={r['current_price']} updated={r['updated_at']}")
print()
print("distinct TON prices in signals(trades rejected) today:")
rows=[float(x[0]) for x in con.execute("SELECT DISTINCT entry_price FROM trades WHERE symbol='TONUSDT' AND entry_time>='2026-11-05'")]
print(sorted(rows))
PY
echo
echo "=== rejects per day (DB volume) ==="
python3 - <<'PY'
import sqlite3
con=sqlite3.connect("data/bot.db")
for d,n in con.execute("SELECT substr(entry_time,1,10) d, COUNT(*) FROM trades WHERE status='rejected' GROUP BY d ORDER BY d DESC LIMIT 6"):
    print(f"{d}  rejected={n}")
print("total rows:", con.execute("SELECT COUNT(*) FROM trades").fetchone()[0])
PY
echo
echo "=== containers ==="
docker ps --format '{{.Names}}\t{{.Status}}' | grep -i replit
echo
echo "=== last ban line ==="
grep -h "IP banned" bot/logs/testnet/api_*.log 2>/dev/null | tail -3
echo
echo "=== bans today by hour ==="
grep -h "IP banned" bot/logs/testnet/api_*.log 2>/dev/null | grep "2026-11-06" | cut -c1-13 | sort | uniq -c
echo
echo "=== exchange open positions (via API) ==="
curl -s http://localhost:5000/api/live/positions 2>/dev/null | head -c 2000
echo
echo "=== /api/binance-sync/positions ==="
curl -s http://localhost:5000/api/binance-sync/positions 2>/dev/null | head -c 2000
echo
echo "=== /api/live/equity ==="
curl -s http://localhost:5000/api/live/equity 2>/dev/null | head -c 1000
