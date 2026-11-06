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
        info = await c.futures_exchange_info()
        for s in info['symbols']:
            if s['symbol'] in ('DOGEUSDT','APTUSDT','MTLUSDT','ETHUSDT'):
                f = {x['filterType']: x for x in s['filters']}
                print(s['symbol'], 'minQty', f.get('LOT_SIZE',{}).get('minQty'),
                      'minNotional', f.get('MIN_NOTIONAL',{}).get('notional'),
                      'tickSize', f.get('PRICE_FILTER',{}).get('tickSize'))
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== где задан floor \$5 ==="
grep -n "minNotional\|MIN_NOTIONAL\|min_notional" bot/order_manager.py | head -30
