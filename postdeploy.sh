#!/bin/bash
cd /opt/trading-bot
date -u
echo "=== боты после рестарта ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
bad = []
for r in con.execute("SELECT symbol,is_running,armed,last_heartbeat FROM bots"):
    try:
        et = datetime.datetime.fromisoformat(str(r['last_heartbeat']).replace('Z','+00:00'))
        if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
        age = (now-et).total_seconds()/60
    except Exception:
        age = 999
    if not r['is_running'] or not r['armed'] or age > 15:
        bad.append((r['symbol'], r['is_running'], r['armed'], round(age,1)))
print("ботов:", con.execute("SELECT COUNT(*) FROM bots").fetchone()[0], "| проблемных:", len(bad), bad[:8])
PY
echo
echo "=== позиции БД vs биржа ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
db = {r['symbol']: (r['direction'], abs(float(r['qty']))) for r in con.execute(
    "SELECT symbol,direction,qty FROM trades WHERE is_open=1 AND status!='rejected'")}
print("БД:", {k: (v[0], round(v[1],4)) for k,v in db.items()})
PY
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -12
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for p in await c.futures_position_information():
            amt = float(p.get('positionAmt', 0) or 0)
            if abs(amt) > 0:
                print(f"POS {p['symbol']:<10} amt={amt} entry={p['entryPrice']} upnl={p.get('unRealizedProfit')}")
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== ошибки за последние 15 минут ==="
grep -h "ERROR" bot/logs/testnet/api_*.log 2>/dev/null | grep -E "2026-11-04 (2[0-2]):" | tail -8
echo "кол-во: $(grep -h 'ERROR' bot/logs/testnet/api_*.log 2>/dev/null | grep -cE '2026-11-04 (2[0-2]):')"
echo
echo "=== новые маркеры в коде (проверка версии) ==="
grep -c "TP_REJECT_COOLDOWN_SEC" bot/order_manager.py
grep -c "leg below minNotional" bot/order_manager.py
