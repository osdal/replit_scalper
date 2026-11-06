#!/bin/bash
cd /opt/trading-bot
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -40
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
from binance.exceptions import BinanceAPIException

async def try_order(c, qty, price, label):
    try:
        r = await c.futures_create_order(symbol='LINKUSDT', side='BUY', type='LIMIT',
                                         price=str(price), quantity=str(qty),
                                         timeInForce='GTC', reduceOnly=True)
        print(f"[{label}] ACCEPTED orderId={r.get('orderId')} qty={qty} price={price}")
        try:
            await c.futures_cancel_order(symbol='LINKUSDT', orderId=r.get('orderId'))
            print(f"[{label}] cancelled (test cleanup)")
        except Exception as e:
            print(f"[{label}] cancel failed: {e}")
        return True
    except BinanceAPIException as e:
        print(f"[{label}] REJECTED code={e.code} msg={e.message} qty={qty} price={price}")
    except Exception as e:
        print(f"[{label}] ERROR {type(e).__name__}: {e}")
    return False

async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for p in await c.futures_position_information():
            if p['symbol'] == 'LINKUSDT':
                print("POS amt=", p['positionAmt'], "entry=", p['entryPrice'],
                      "notional=", p.get('notional'), "mark=", p.get('markPrice'))
        for p in await c.futures_position_information():
            pass
        # сколько биржа считает доступным к закрытию
        try:
            pr = await c.futures_position_information(symbol='LINKUSDT')
            print("POS RAW:", {k: pr[0].get(k) for k in ('positionAmt','entryPrice','notional','isolatedWallet','markPrice')})
        except Exception as e:
            print("POS RAW ERR", e)

        print("--- тест 1: как делает бот (0.37 @ 10.862) ---")
        await try_order(c, 0.37, '10.8620', 't1')
        print("--- тест 2: чуть меньше (0.36 @ 10.862) ---")
        await try_order(c, 0.36, '10.8620', 't2')
        print("--- тест 3: без reduceOnly (0.37 @ 10.862) ---")
        try:
            r = await c.futures_create_order(symbol='LINKUSDT', side='BUY', type='LIMIT',
                                             price='10.8620', quantity='0.37', timeInForce='GTC')
            print("[t3] ACCEPTED orderId=", r.get('orderId'))
            await c.futures_cancel_order(symbol='LINKUSDT', orderId=r.get('orderId'))
            print("[t3] cancelled")
        except BinanceAPIException as e:
            print(f"[t3] REJECTED code={e.code} msg={e.message}")
        print("--- итоговые ордера ---")
        for o in await c.futures_get_open_orders(symbol='LINKUSDT'):
            print(" ORD", o.get('type'), o.get('side'), o.get('price'), o.get('origQty'), o.get('reduceOnly'))
        for a in await c.futures_get_open_algo_orders(symbol='LINKUSDT') or []:
            print(" ALG", a.get('orderType'), a.get('side'), a.get('triggerPrice'), a.get('closePosition'))
    finally:
        await c.close_connection()
asyncio.run(main())
PY
