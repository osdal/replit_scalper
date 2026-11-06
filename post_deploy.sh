#!/bin/bash
sleep 20
cd /opt/trading-bot
echo "=== container ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'
echo
echo "=== bots ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
rows = con.execute("SELECT symbol,is_running,armed,last_heartbeat FROM bots ORDER BY symbol").fetchall()
bad = []
for r in rows:
    ts = r['last_heartbeat']
    age = None
    if ts:
        try:
            et = datetime.datetime.fromisoformat(str(ts).replace('Z','+00:00'))
            if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
            age = (now-et).total_seconds()/60
        except Exception: pass
    if not r['is_running'] or not r['armed'] or age is None or age > 10:
        bad.append((r['symbol'], r['is_running'], r['armed'], ts))
print("всего:", len(rows), "| проблемных:", len(bad))
for b in bad[:20]: print("  ", b)
print("открытых accepted:", con.execute("SELECT COUNT(*) FROM trades WHERE is_open=1 AND status!='rejected'").fetchone()[0])
PY
echo
echo "=== /trading/check ETH ==="
curl -s -X POST http://localhost:5000/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"ETHUSDT"}'
echo
echo "=== ERROR после рестарта ==="
docker logs replit_scalper-api-testnet --since 2m 2>&1 | grep -E "ERROR|Traceback" | head -10
