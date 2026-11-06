#!/bin/bash
cd /opt/trading-bot
echo "=== LINK: лог 20:00-20:25 ==="
grep -hE "2026-11-03 20:(0[0-9]|1[0-9]|2[0-5])" bot/logs/testnet/api_linkusdt_stdout.log 2>/dev/null | grep -vE "RATE_LIMIT|Heartbeat|PRICE\]" | tail -40
echo
echo "=== Ордера по всем открытым символам ==="
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -30
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for s in ('DOGEUSDT','APTUSDT','LINKUSDT','NEARUSDT','DOTUSDT'):
            o = await c.futures_get_open_orders(symbol=s)
            print('ORD', s, [(x.get('type'), x.get('side'), x.get('price'), x.get('stopPrice'), x.get('origQty'), x.get('reduceOnly'), x.get('closePosition')) for x in o])
            try:
                a = await c.futures_get_open_algo_orders(symbol=s)
                print('ALGO', s, [(x.get('algoType'), x.get('side'), x.get('triggerPrice'), x.get('closePosition'), x.get('algoId')) for x in (a or [])])
            except Exception as e:
                print('ALGO', s, 'ERR', e)
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== LINK state ==="
cat bot/state_testnet_linkusdt.json 2>/dev/null
