import ccxt.async_support as ccxt
import asyncio
import os
from typing import Optional

KUCOIN_API_KEY = os.getenv("KUCOIN_API_KEY", "")
KUCOIN_API_SECRET = os.getenv("KUCOIN_API_SECRET", "")
KUCOIN_API_PASSPHRASE = os.getenv("KUCOIN_API_PASSPHRASE", "")

async def get_client():
    if not KUCOIN_API_KEY:
        raise ValueError("KUCOIN_API_KEY not set in environment")
    return ccxt.kucoinfutures({
        'apiKey': KUCOIN_API_KEY,
        'secret': KUCOIN_API_SECRET,
        'password': KUCOIN_API_PASSPHRASE,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'future',
            'adjustForTimeDifference': True,
        },
    })

async def get_balance(symbol: str = "USDT") -> float:
    client = await get_client()
    try:
        balance = await client.fetch_balance()
        return float(balance.get('total', {}).get(symbol, 0))
    finally:
        await client.close()

async def get_price(symbol: str) -> float:
    client = await get_client()
    try:
        ticker = await client.fetch_ticker(symbol)
        return float(ticker['last'])
    finally:
        await client.close()

async def place_order(
    symbol: str,
    side: str,  # "buy" or "sell"
    order_type: str,  # "market" or "limit"
    amount: float,
    price: Optional[float] = None,
):
    client = await get_client()
    try:
        params = {}
        if order_type == "market":
            order = await client.create_order(symbol, 'market', side, amount, None, params)
        else:
            order = await client.create_order(symbol, 'limit', side, amount, price, params)
        return order
    finally:
        await client.close()

async def place_tp_order(symbol: str, side: str, amount: float, tp_price: float):
    client = await get_client()
    try:
        order = await client.create_order(symbol, 'limit', side, amount, tp_price, {'reduceOnly': True})
        return order
    finally:
        await client.close()