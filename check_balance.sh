#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== balance cache in container ==="
docker exec replit_scalper-api-testnet bash -lc 'ls -la /app/bot/data/*.json 2>/dev/null | grep -i balance; echo "---"; cat /app/bot/data/balance_cache_testnet.json 2>/dev/null || echo "no balance cache"'
echo
echo "=== testnet env for balance ==="
docker exec replit_scalper-api-testnet bash -lc 'env | grep -iE "balance|deposit|paper|mode"'
echo
echo "=== check a config for mode ==="
head -30 bot/configs/testnet/config_lsk.yaml
echo
echo "=== testnet balance API call ==="
docker exec replit_scalper-api-testnet bash -lc 'python3 - <<"PY"
import asyncio, os
from dotenv import load_dotenv
load_dotenv("/app/.env", override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv("BINANCE_API_KEY"), api_secret=os.getenv("BINANCE_API_SECRET"), testnet=True)
    try:
        acc = await c.futures_account()
        print("totalWalletBalance:", acc.get("totalWalletBalance"))
        print("availableBalance:", acc.get("availableBalance"))
        assets = acc.get("assets", [])
        for a in assets:
            if a["asset"] == "USDT":
                print("USDT:", a)
    finally:
        await c.close_connection()
asyncio.run(main())
PY
' 2>&1