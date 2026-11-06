#!/bin/bash
cd /opt/trading-bot
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -25
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
from binance.exceptions import BinanceAPIException

async def t(c, sym, side, qty, price, label):
    try:
        r = await c.futures_create_order(symbol=sym, side=side, type='LIMIT', price=price,
                                         quantity=qty, timeInForce='GTC', reduceOnly=True)
        print(f"[{label}] ACCEPTED id={r.get('orderId')}")
        await c.futures_cancel_order(symbol=sym, orderId=r.get('orderId'))
        print(f"[{label}] cancelled")
    except BinanceAPIException as e:
        print(f"[{label}] REJECTED code={e.code} msg={e.message}")

async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        # APT: есть и closePosition-стоп, и рабочий reduceOnly TP
        await t(c, 'APTUSDT', 'BUY', '1', '0.6000', 'APT 1@0.60 (стоп+TP есть)')
        # NEAR: есть и closePosition-стоп, и рабочий reduceOnly TP
        await t(c, 'NEARUSDT', 'SELL', '0.1', '4.0000', 'NEAR 0.1@4.00 (стоп+TP есть)')
        # LINK: только closePosition-стоп
        await t(c, 'LINKUSDT', 'BUY', '0.01', '10.8620', 'LINK 0.01@10.862 (только стоп)')
        print("--- ордера после теста ---")
        for s in ('APTUSDT','NEARUSDT','LINKUSDT'):
            o = await c.futures_get_open_orders(symbol=s)
            print(s, 'обычных', len(o), [(x.get('type'), x.get('side'), x.get('price'), x.get('origQty')) for x in o])
    finally:
        await c.close_connection()
asyncio.run(main())
PY
