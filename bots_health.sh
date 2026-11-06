#!/bin/bash
cd /opt/trading-bot
echo "=== 1. Ордера на бирже (в контейнере) ==="
docker exec -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -20
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for s in ('DOGEUSDT','APTUSDT','MTLUSDT'):
            try:
                orders = await c.futures_get_open_orders(symbol=s)
            except Exception as e:
                print(s, 'ERR', e); continue
            print(s, 'regular:', [(o['type'], o['side'], o['price'], o['stopPrice'], o['closePosition']) for o in orders])
            try:
                algos = await c.futures_get_open_algo_orders(symbol=s)
                print(s, 'algo:', [(a.get('algoType'), a.get('side'), a.get('triggerPrice'), a.get('closePosition')) for a in (algos or [])])
            except Exception as e:
                print(s, 'algo ERR', e)
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== 2. Свежесть heartbeat по ботам (БД) ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
cols = [r[1] for r in con.execute("PRAGMA table_info(bots)")]
print("cols:", cols)
now = datetime.datetime.now(datetime.timezone.utc)
rows = con.execute("SELECT * FROM bots ORDER BY symbol").fetchall()
stale = []
for r in rows:
    d = dict(r)
    ts = d.get('last_heartbeat') or d.get('last_seen') or d.get('updated_at')
    age = None
    if ts:
        try:
            et = datetime.datetime.fromisoformat(str(ts).replace('Z','+00:00'))
            if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
            age = (now-et).total_seconds()/60
        except Exception: pass
    if age is None or age > 10 or not (d.get('is_running') or d.get('armed')):
        stale.append((d.get('symbol'), d.get('is_running'), d.get('armed'), ts, None if age is None else round(age,1)))
print("всего ботов:", len(rows))
print("подозрительные (нет heartbeat >10мин / не running / не armed):")
for x in stale: print(" ", x)
PY
echo
echo "=== 3. Процессы ботов в контейнере ==="
docker exec replit_scalper-api-testnet sh -c 'ps -eo pid,etime,args | grep -c "main.py"'
docker exec replit_scalper-api-testnet sh -c 'ps -eo pid,etime,args | grep "main.py" | grep -E "dotusdt|filusdt|hbarusdt|imxusdt|dogeusdt" | head'
echo
echo "=== 4. Свежие heartbeat в логах DOT/FIL/HBAR/IMX ==="
for s in dotusdt filusdt hbarusdt imxusdt; do
  echo "--- $s"
  grep -h "Heartbeat" bot/logs/testnet/api_${s}_stdout.log 2>/dev/null | tail -2
done
