#!/bin/bash
cd /opt/trading-bot
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -15
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for s in ('ETHUSDT','NEARUSDT','SUIUSDT'):
            o = await c.futures_get_open_orders(symbol=s)
            a = await c.futures_get_open_algo_orders(symbol=s) or []
            print(s, '| TP:', [(x['type'], x['side'], x['price'], x['origQty']) for x in o],
                  '| SL:', [(x['orderType'], x['side'], x['triggerPrice']) for x in a])
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== -2022 / -4164 после деплоя (21:21+) ==="
echo "-2022: $(grep -h '\-2022' bot/logs/testnet/api_*.log | grep -cE '2026-11-04 21:(2[1-9]|3[0-9])')"
echo "-4164: $(grep -h '\-4164' bot/logs/testnet/api_*.log | grep -cE '2026-11-04 21:(2[1-9]|3[0-9])')"
echo "новые маркеры: $(grep -h 'leg below minNotional' bot/logs/testnet/api_*.log | grep -c '2026-11-04')"
