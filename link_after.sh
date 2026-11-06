#!/bin/bash
cd /opt/trading-bot
date -u
echo "=== LINK: последние 25 строк лога ==="
tail -25 bot/logs/testnet/api_linkusdt_stdout.log | grep -vE "PRICE\]|Heartbeat"
echo
echo "=== LINK: все BACKSTOP/TP события за последние 2 часа ==="
grep -hE "BACKSTOP|TP reduceOnly|chain TP|TP limit placed|EXPIRED|SL hit" bot/logs/testnet/api_linkusdt_stdout.log | tail -15
echo
echo "=== LINK: ордера сейчас ==="
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -12
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for o in await c.futures_get_open_orders(symbol='LINKUSDT'):
            print(' ORD', o.get('type'), o.get('side'), o.get('price'), o.get('origQty'), o.get('reduceOnly'))
        for a in await c.futures_get_open_algo_orders(symbol='LINKUSDT') or []:
            print(' ALG', a.get('orderType'), a.get('side'), a.get('triggerPrice'), a.get('closePosition'), a.get('algoId'))
        for p in await c.futures_position_information(symbol='LINKUSDT'):
            print(' POS', p['positionAmt'], p['entryPrice'], p.get('markPrice'), p.get('notional'), p.get('unRealizedProfit'))
        try:
            m = await c.futures_get_position_mode()
            print(' POSITION MODE', m)
        except Exception as e:
            print(' POSITION MODE ERR', e)
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== LINK state: backstop id ==="
python3 -c "import json;d=json.load(open('bot/state_testnet_linkusdt.json'));print('backstop_algo_id',d.get('backstop_algo_id'),'sl',d.get('sl_price'),'tp',d.get('tp1_price'),'step',d.get('reverse_chain_step'))"
