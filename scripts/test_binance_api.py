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
    try:
        p = await c.futures_position_information(symbol="BTCUSDT")
        print("OK", len(p))
        for pos in p:
            print(pos)
    except Exception as e:
        print("ERROR", e)
    await c.close_connection()

asyncio.run(main())
