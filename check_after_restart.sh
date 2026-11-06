#!/bin/bash
cd /opt/trading-bot
date -u
echo "=== git ==="
git log --oneline -1
git status --short -- bot/ | head -5
echo
echo "=== контейнер ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'
echo
echo "=== маркеры в коде на хосте/в контейнере ==="
echo "host dust_offset: $(grep -c 'dust_offset_qty' bot/order_manager.py) $(grep -c 'dust_offset_qty' bot/main.py)"
docker exec replit_scalper-api-testnet bash -lc 'echo "container dust_offset: $(grep -c dust_offset_qty /app/bot/order_manager.py) $(grep -c dust_offset_qty /app/bot/main.py)"' 2>&1 | tail -2
echo
echo "=== боты ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
bad = []
ages = []
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
if ages:
    print("медиана heartbeat, мин:", round(sorted(ages)[len(ages)//2],1), "| худший:", round(max(ages),1))
PY
echo
echo "=== открытые строки БД / биржа ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,symbol,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,entry_time
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY symbol,id"""):
    print(dict(r))
PY
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -8
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for p in await c.futures_position_information():
            a = float(p.get('positionAmt', 0) or 0)
            if abs(a) > 0:
                print(f"{p['symbol']:<10} amt={a} entry={p['entryPrice']} upnl={p.get('unRealizedProfit')}")
    finally:
        await c.close_connection()
asyncio.run(main())
PY
