#!/bin/bash
cd /opt/trading-bot
date -u
echo "=== git ==="
git log --oneline -1
echo
echo "=== status ==="
curl -s --max-time 10 http://localhost:5000/api/trading/status | python3 -m json.tool 2>/dev/null | head -25
echo
echo "=== check (информационный) ==="
curl -s --max-time 10 -X POST http://localhost:5000/api/trading/check \
  -H 'Content-Type: application/json' -d '{"symbol":"ETHUSDT"}' | python3 -m json.tool 2>/dev/null | head -20
echo
echo "=== боты ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
bad = []; ages = []
for r in con.execute("SELECT symbol,is_running,armed,last_heartbeat FROM bots"):
    try:
        et = datetime.datetime.fromisoformat(str(r['last_heartbeat']).replace('Z','+00:00'))
        if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
        age = (now-et).total_seconds()/60
    except Exception:
        age = 999
    ages.append(age)
    if not r['is_running'] or not r['armed'] or age > 15:
        bad.append((r['symbol'], r['is_running'], r['armed'], round(age,1)))
print("ботов:", len(ages), "| проблемных:", len(bad), bad[:6])
if ages: print("медиана heartbeat:", round(sorted(ages)[len(ages)//2],1), "мин")
PY
echo
echo "=== trading/check ошибки за 15 мин ==="
grep -h "trading/check error" bot/logs/testnet/api_*.log | grep -E "2026-11-05 (07:2[0-9]|07:3[0-9])" | tail -5
echo "кол-во: $(grep -h 'trading/check error' bot/logs/testnet/api_*.log | grep -cE '2026-11-05 07:(2[0-9]|3[0-9])')"
echo
echo "=== версия кода API в контейнере ==="
docker exec replit_scalper-api-testnet bash -lc "grep -c 'getDailyNet' /app/artifacts/api-server/src/routes/trading.ts; grep -c 'daily_net' /app/artifacts/api-server/src/routes/trading.ts" 2>&1 | tail -2
