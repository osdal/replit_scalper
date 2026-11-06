#!/bin/bash
cd /opt/trading-bot
echo "=== 1. DOGE лог с 11:10 ==="
grep -hE "2026-11-03 11:(1[0-9]|2[0-9]|3[0-9])" bot/logs/testnet/api_dogeusdt_stdout.log 2>/dev/null | grep -vE "RATE_LIMIT|Heartbeat|PRICE\]" | tail -50
echo
echo "=== 2. DOGE: strata/sizing/reverse ==="
grep -hE "REVERSE|Size by|Market order|backstop|BACKSTOP|TP|SL|stop" bot/logs/testnet/api_dogeusdt_stdout.log 2>/dev/null | grep "2026-11-03" | tail -40
echo
echo "=== 3. ERROR 03.11 (все) ==="
grep -h "ERROR" bot/logs/testnet/api_*.log 2>/dev/null | grep "2026-11-03" | tail -25
echo
echo "=== 4. Ордера DOGE/MTL (правильные поля) ==="
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -20
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for s in ('DOGEUSDT','MTLUSDT'):
            o = await c.futures_get_open_orders(symbol=s)
            print('ORD', s, [(x.get('type'), x.get('side'), x.get('price'), x.get('stopPrice'), x.get('origQty'), x.get('closePosition')) for x in o])
            try:
                a = await c.futures_get_open_algo_orders(symbol=s)
                print('ALGO', s, [(x.get('algoType'), x.get('side'), x.get('triggerPrice'), x.get('closePosition')) for x in (a or [])])
            except Exception as e:
                print('ALGO', s, 'ERR', e)
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== 5. DOGE строка в БД полностью ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
r = con.execute("SELECT * FROM trades WHERE id=191416").fetchone()
for k in r.keys():
    print(f"  {k} = {r[k]}")
PY
