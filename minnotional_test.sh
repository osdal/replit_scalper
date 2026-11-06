#!/bin/bash
cd /opt/trading-bot
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -20
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
from binance.exceptions import BinanceAPIException

async def t(c, sym, side, qty, price, label):
    try:
        r = await c.futures_create_order(symbol=sym, side=side, type='LIMIT', price=price,
                                         quantity=qty, timeInForce='GTC', reduceOnly=True)
        print(f"[{label}] ACCEPTED notional={float(qty)*float(price):.2f} id={r.get('orderId')}")
        await c.futures_cancel_order(symbol=sym, orderId=r.get('orderId'))
        print(f"[{label}] cancelled")
    except BinanceAPIException as e:
        print(f"[{label}] REJECTED notional={float(qty)*float(price):.2f} code={e.code} msg={e.message}")

async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        # APT SHORT 13.2 @ 0.616: цена 0.60 -> $6.00 (выше minNotional $5), частичный объём
        await t(c, 'APTUSDT', 'BUY', '10', '0.6000', 'APT 10@0.60 = $6.00')
        # APT: 13.2 @ 0.60 -> $7.92 (весь объём)
        await t(c, 'APTUSDT', 'BUY', '13.2', '0.6000', 'APT 13.2@0.60 = $7.92')
        # APT: 8 @ 0.60 -> $4.80 (чуть ниже $5)
        await t(c, 'APTUSDT', 'BUY', '8', '0.6000', 'APT 8@0.60 = $4.80')
    finally:
        await c.close_connection()
asyncio.run(main())
PY
