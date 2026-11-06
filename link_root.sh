#!/bin/bash
cd /opt/trading-bot
echo "=== 1. Ордера по всем 5 открытым ==="
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -40
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
            print(f"{s}: обычных ордеров {len(o)}")
            for x in o:
                print("   ORD", x.get('type'), x.get('side'), 'price', x.get('price'),
                      'stop', x.get('stopPrice'), 'qty', x.get('origQty'),
                      'reduceOnly', x.get('reduceOnly'), 'closePos', x.get('closePosition'))
            try:
                a = await c.futures_get_open_algo_orders(symbol=s) or []
                print(f"   ALGO {len(a)}")
                for x in a:
                    print("   ALG", x.get('orderType'), x.get('side'), 'trig', x.get('triggerPrice'),
                          'closePos', x.get('closePosition'), x.get('algoId'))
            except Exception as e:
                print("   ALGO ERR", e)
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== 2. Полные строки -2022 с контекстом (LINK) ==="
grep -n "\-2022" bot/logs/testnet/api_linkusdt_stdout.log | tail -4
echo
echo "--- сырой текст ошибки, если есть exc_info ---"
grep -nE "code=-2022|ReduceOnly Order|APIError|BinanceAPIException" bot/logs/testnet/api_linkusdt_stdout.log | tail -8
echo
echo "=== 3. LINK: что делает бот каждую свечу (21:00-21:40) ==="
grep -hE "2026-11-03 21:(0|1|2|3)[0-9]:" bot/logs/testnet/api_linkusdt_stdout.log | grep -vE "Candle closed|Heartbeat|PRICE\]|RATE_LIMIT" | tail -25
echo
echo "=== 4. Счётчик -2022 по часам за 03.11 ==="
grep -h "\-2022" bot/logs/testnet/api_*.log | grep "2026-11-03" | cut -c1-13 | sort | uniq -c
