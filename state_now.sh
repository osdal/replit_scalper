#!/bin/bash
cd /opt/trading-bot
echo "=== 1. Сейчас (UTC) ==="
date -u
echo
echo "=== 2. Открытые accepted в БД ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
cur = con.execute("""SELECT id,symbol,direction,ROUND(qty,6) qty,ROUND(entry_price,6) entry,
       ROUND(qty*entry_price,2) notional, ROUND(pnl,4) pnl,
       ROUND((julianday('now')-julianday(entry_time))*24,1) age_h, status
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time""")
for r in cur.fetchall(): print(dict(r))
PY
echo
echo "=== 3. Закрытия за 03.11 ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
cur = con.execute("""SELECT symbol, exit_reason, ROUND(pnl,4) pnl, ROUND(qty*entry_price,2) notional, exit_time
FROM trades WHERE exit_time >= '2026-11-03' AND is_open=0 AND status!='rejected' ORDER BY exit_time""")
rows = cur.fetchall()
for r in rows: print(dict(r))
print("итого закрытий:", len(rows), "pnl=", round(sum(r['pnl'] or 0 for r in rows), 4))
PY
echo
echo "=== 4. /trading/check (ETH) ==="
curl -s -X POST http://localhost:5000/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"ETHUSDT"}'
echo
echo "=== 5. Контейнер / боты ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
print("bots running/armed:", con.execute("SELECT COUNT(*), SUM(is_running), SUM(armed) FROM bots").fetchone())
PY
echo
echo "=== 6. Последние входы (Market order placed) ==="
grep -h "Market order placed" bot/logs/testnet/api_*.log 2>/dev/null | tail -8
echo
echo "=== 7. Ошибки в логах за 03.11 ==="
grep -h "ERROR" bot/logs/testnet/api_*.log 2>/dev/null | grep "2026-11-03" | tail -10
echo "(всего ERROR 03.11: $(grep -h "ERROR" bot/logs/testnet/api_*.log 2>/dev/null | grep -c "2026-11-03"))"
