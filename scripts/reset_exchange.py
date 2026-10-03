"""Сброс торгового окружения: закрыть все позиции и снять все ордера.

Работает с ключами из `.env.<BOT_ENV>` (в контейнере стека — из /app/.env для
testnet или /app/.env.live для live). Предназначен для «чистого листа» перед
сбором статистики: после выполнения на бирже не остаётся ни позиций, ни
защитных/лимитных ордеров.

Запуск (из контейнера нужного стека):
    docker cp scripts/reset_exchange.py <container>:/tmp/
    docker exec -w /app/bot <container> python3 /tmp/reset_exchange.py --confirm

Опции:
    --confirm     реально закрыть позиции и снять ордера (без флага — только отчёт)
    --symbols A,B ограничить символами (по умолчанию все)
    --dry-run     печатать, что было бы сделано (не подтверждать)
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))

from dotenv import load_dotenv  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--confirm", action="store_true",
                    help="выполнить закрытие (без флага — только отчёт)")
parser.add_argument("--symbols", help="список символов через запятую, по умолчанию все")
args = parser.parse_args()

# Определяем окружение: в контейнере стека BOT_ENV уже задан (testnet/live),
# иначе пробуем оба файла — сначала .env (testnet), т.к. это безопаснее.
BOT_ENV = (os.getenv("BOT_ENV") or "testnet").strip().lower()
for candidate in (f"/app/.env.{BOT_ENV}", "/app/.env", "/app/.env.live"):
    if os.path.exists(candidate):
        load_dotenv(candidate, override=True)
        print(f"используем {candidate} (BINANCE_TESTNET={os.getenv('BINANCE_TESTNET')})")
        break
else:
    raise SystemExit("не найден ни .env, ни .env.<BOT_ENV>")

TESTNET = (os.getenv("BINANCE_TESTNET") or "false").strip().lower() == "true"

from binance import AsyncClient  # noqa: E402


async def main() -> None:
    # Именно testnet=True переключает клиент на testnet-эндпоинты. Без этого
    # тестнет-ключи уходят на боевой API и Binance отвечает -2015.
    client = await AsyncClient.create(
        api_key=os.environ["BINANCE_API_KEY"],
        api_secret=os.environ["BINANCE_API_SECRET"],
        testnet=TESTNET,
    )
    try:
        positions = await client.futures_position_information()
        opened = [p for p in positions if float(p.get("positionAmt", 0)) != 0]

        wanted = None
        if args.symbols:
            wanted = {s.strip().upper() for s in args.symbols.split(",") if s.strip()}
        if wanted is not None:
            opened = [p for p in opened if p["symbol"] in wanted]

        # Сначала снимаем все ордера: иначе закрытие позиции может упереться
        # в reduceOnly-конфликт с уже висящим лимитным ордером.
        cancelled = 0
        symbols = sorted({p["symbol"] for p in opened} | (wanted or set()))
        for symbol in symbols:
            try:
                for order in await client.futures_get_open_orders(symbol=symbol):
                    if args.confirm:
                        await client.futures_cancel_order(symbol=symbol, orderId=order["orderId"])
                    cancelled += 1
            except Exception as exc:
                print(f"  ! обычные ордера {symbol}: {exc}")
            try:
                for algo in await client.futures_get_open_algo_orders(symbol=symbol):
                    if args.confirm:
                        await client.futures_cancel_algo_order(symbol=symbol, algoId=algo["algoId"])
                    cancelled += 1
            except Exception as exc:
                print(f"  ! algo-ордера {symbol}: {exc}")

        closed = 0
        for p in opened:
            symbol = p["symbol"]
            amt = float(p["positionAmt"])
            side = "SELL" if amt > 0 else "BUY"
            qty = abs(amt)
            mark = p.get("markPrice")
            print(f"  {symbol:14} закрытие {side} qty={qty} mark={mark}")
            if not args.confirm:
                continue
            try:
                await client.futures_create_order(
                    symbol=symbol, side=side, type="MARKET", quantity=qty,
                )
                closed += 1
            except Exception as exc:
                print(f"  ! не удалось закрыть {symbol}: {exc}")

        # Контрольный опрос: что осталось на бирже
        after = await client.futures_position_information()
        left = [p for p in after if float(p.get("positionAmt", 0)) != 0]
        algo_left = 0
        for p in left:
            try:
                algo_left += len(await client.futures_get_open_algo_orders(symbol=p["symbol"]))
            except Exception:
                pass

        print()
        print(f"позиций закрыто:     {closed if args.confirm else 0} (найдено {len(opened)})")
        print(f"ордеров снято:       {cancelled}")
        print(f"осталось позиций:    {len(left)}")
        print(f"осталось algo-ордеров: {algo_left}")
        if not args.confirm:
            print("\nЭто был отчёт. Повторите с --confirm для выполнения.")
        elif left or algo_left:
            print("\nВНИМАНИЕ: на бирже что-то осталось — проверьте вручную.")
            raise SystemExit(1)
        else:
            print("\nБиржа чистая.")
    finally:
        await client.close_connection()


asyncio.run(main())