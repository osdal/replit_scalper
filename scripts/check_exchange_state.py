"""Проверка реального состояния на Binance Futures: позиции и биржевые SL/TP.

Читает ключи из `.env.<BOT_ENV>` (по умолчанию `.env.live`), поэтому запускать
нужно с тем окружением, которое хотите проверить — например, внутри контейнера
live-стека:

    docker cp scripts/check_exchange_state.py <container>:/tmp/
    docker exec -w /app/bot <container> python3 /tmp/check_exchange_state.py

Полезно при переезде на другую машину: показывает, что реально есть на бирже,
даже если боты не запущены.

Опции:
    --symbol SYM   показать algo-ордера только по этой паре
    --flat         вывести и позиции с нулевым объёмом
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))

from dotenv import load_dotenv  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--symbol", help="показать algo-ордера только по этой паре")
parser.add_argument("--flat", action="store_true", help="показать и пустые позиции")
args = parser.parse_args()

load_dotenv("/app/.env.live", override=True)

from binance import AsyncClient  # noqa: E402


async def main() -> None:
    client = await AsyncClient.create(
        api_key=os.environ["BINANCE_API_KEY"],
        api_secret=os.environ["BINANCE_API_SECRET"],
    )
    try:
        print(f"BINANCE_TESTNET={os.environ.get('BINANCE_TESTNET')}")
        positions = await client.futures_position_information()
        opened = [p for p in positions if float(p.get("positionAmt", 0)) != 0]

        if not opened:
            print("Открытых позиций нет.")
        for p in opened if opened else (positions if args.flat else []):
            amt = float(p.get("positionAmt", 0))
            side = "LONG" if amt > 0 else "SHORT"
            print(f"  {p['symbol']:14} {side:5} qty={abs(amt):<12} "
                  f"entry={p.get('entryPrice')} uPNL={p.get('unRealizedProfit')}")

        symbols = [args.symbol] if args.symbol else [p["symbol"] for p in opened]
        for symbol in symbols:
            try:
                algos = await client.futures_get_open_algo_orders(symbol=symbol)
            except Exception as exc:  # нет доступа/пары — не критично
                print(f"  algo-ордера {symbol}: не удалось получить ({exc})")
                continue
            if not algos:
                continue
            print(f"  Биржевые ордера {symbol}:")
            for a in algos:
                print(f"    algoId={a.get('algoId')} side={a.get('side')} "
                      f"qty={a.get('quantity')} trigger={a.get('triggerPrice')} "
                      f"type={a.get('orderType')}")
    finally:
        await client.close_connection()


asyncio.run(main())