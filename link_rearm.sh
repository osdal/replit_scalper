#!/bin/bash
cd /opt/trading-bot
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -30
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
from binance.exceptions import BinanceAPIException

SYM = 'LINKUSDT'

async def stop():
    return await c.futures_get_open_algo_orders(symbol=SYM) or []

async def main():
    global c
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        algs = await stop()
        print("ALGO до:", [(a['algoId'], a['orderType'], a['side'], a['triggerPrice'], a['closePosition']) for a in algs])
        # 1. снимаем closePosition-стоп
        for a in algs:
            await c.futures_cancel_algo_order(symbol=SYM, algoId=a['algoId'])
            print("снят стоп", a['algoId'])
        await asyncio.sleep(1)
        # 2. пробуем reduceOnly TP-лимит
        ok = False
        try:
            r = await c.futures_create_order(symbol=SYM, side='BUY', type='LIMIT',
                                             price='10.8620', quantity='0.37',
                                             timeInForce='GTC', reduceOnly=True)
            ok = True
            print("TP ACCEPTED orderId=", r.get('orderId'), "price", r.get('price'), "qty", r.get('origQty'))
        except BinanceAPIException as e:
            print("TP REJECTED code=", e.code, "msg=", e.message)
        # 3. возвращаем стоп (closePosition) — теперь после TP
        try:
            s = await c.futures_create_algo_order(algoType='CONDITIONAL', symbol=SYM, side='BUY',
                                                  type='STOP_MARKET', triggerPrice='11.4660',
                                                  closePosition='true', workingType='MARK_PRICE',
                                                  clientAlgoId='botsl_LINKUSDT_short')
            print("STOP ACCEPTED algoId=", s.get('algoId'), "trigger", s.get('triggerPrice'))
        except BinanceAPIException as e:
            print("STOP REJECTED code=", e.code, "msg=", e.message)
        # 4. итог
        print("--- итог ---")
        for o in await c.futures_get_open_orders(symbol=SYM):
            print(" ORD", o.get('type'), o.get('side'), o.get('price'), o.get('origQty'), 'reduceOnly', o.get('reduceOnly'))
        for a in await stop():
            print(" ALG", a.get('orderType'), a.get('side'), a.get('triggerPrice'), a.get('closePosition'), a.get('algoId'))
    finally:
        await c.close_connection()
asyncio.run(main())
PY
