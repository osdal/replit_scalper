import asyncio
import os
from binance import AsyncClient
from dotenv import load_dotenv

load_dotenv()

async def main():
    c = await AsyncClient.create(
        os.getenv("BINANCE_API_KEY", ""),
        os.getenv("BINANCE_API_SECRET", ""),
    )
    for sym in ["SOLUSDT", "POLUSDT", "ETHUSDT", "BTCUSDT"]:
        try:
            positions = await c.futures_position_information(symbol=sym)
            found = False
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                if abs(amt) > 0:
                    print(f"{sym}: POSITION amt={amt} entry={p.get('entryPrice')} unrealized={p.get('unrealizedProfit')}")
                    found = True
            if not found:
                print(f"{sym}: no position")
        except Exception as e:
            print(f"{sym}: ERROR {e}")
    await c.close_connection()

asyncio.run(main())
