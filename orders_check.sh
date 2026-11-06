#!/bin/bash
cd /opt/trading-bot
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -30
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
