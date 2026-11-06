#!/bin/bash
sleep 15
cd /opt/trading-bot
echo "=== 1. Кэп в БД ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
print("распределение max_positions_with_expired:", con.execute("SELECT max_positions_with_expired, COUNT(*) FROM bots GROUP BY 1").fetchall())
print("открытые accepted:")
for r in con.execute("SELECT symbol,direction,ROUND(qty,4),ROUND(entry_price,6),ROUND(qty*entry_price,2),is_open,status FROM trades WHERE is_open=1 AND status!='rejected'"):
    print(" ", r)
print("ATOM/DOGE закрыты ботом:")
for r in con.execute("SELECT symbol,status,is_open,ROUND(pnl,4),exit_reason,exit_time FROM trades WHERE symbol IN ('ATOMUSDT','DOGEUSDT') ORDER BY exit_time DESC LIMIT 6"):
    print(" ", r)
PY
echo
echo "=== 2. /trading/check для нового символа (ETH) ==="
curl -s -X POST http://localhost:5000/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"ETHUSDT"}'
echo
echo "=== 3. Контейнер и боты ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
print("bots running/armed:", con.execute("SELECT COUNT(*), SUM(is_running), SUM(armed) FROM bots").fetchone())
PY
echo
echo "=== 4. Ошибки после рестарта ==="
docker logs replit_scalper-api-testnet --since 2m 2>&1 | grep -cE "ERROR|Traceback"
