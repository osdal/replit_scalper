#!/bin/bash
cd /opt/trading-bot
echo "=== 1. bots table: лимиты по нескольким символам ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
cur = con.execute("SELECT symbol, max_positions, max_positions_with_expired, expired_after_minutes FROM bots WHERE symbol IN ('ATOMUSDT','DOGEUSDT','MTLUSDT','BTCUSDT','ETHUSDT') ORDER BY symbol")
for r in cur.fetchall(): print(dict(r))
print("--- распределение значений:")
cur = con.execute("SELECT max_positions_with_expired v, COUNT(*) n FROM bots GROUP BY v")
for r in cur.fetchall(): print(dict(r))
PY
echo
echo "=== 2. MTLUSDT: вход и sizing ==="
grep -hE "Size by|Market order placed|qty raised|CAP\]" logs/testnet/api_mtlusdt_stdout.log 2>/dev/null | tail -20
echo
echo "=== 3. ATOM/DOGE: последние 10 строк каждого ==="
for s in atomusdt dogeusdt; do
  echo "--- $s"
  tail -12 logs/testnet/api_${s}_stdout.log 2>/dev/null
done
echo
echo "=== 4. API /trading/check сейчас (ATOM, MTL) ==="
curl -s -X POST http://localhost:5000/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"ATOMUSDT"}'
echo
curl -s -X POST http://localhost:5000/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"MTLUSDT"}'
echo
echo "=== 5. Боты: сколько процессов и heartbeat ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
try:
    cur = con.execute("SELECT COUNT(*) n, SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) running FROM bots")
    print(dict(cur.fetchone()))
except Exception as e:
    print("no status col:", e)
PY
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'
