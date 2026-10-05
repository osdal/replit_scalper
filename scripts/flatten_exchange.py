"""Полная очистка биржи: закрыть все позиции и снять ВСЕ ордера (обычные и algo).

Работает с ключами из /app/.env.<BOT_ENV> внутри контейнера нужного стека.
Ничего не трогает в другом окружении: биржа определяется переменной
BINANCE_TESTNET, а не хостом.

    docker cp scripts/flatten_exchange.py <container>:/tmp/
    docker exec -w /app/bot <container> python3 /tmp/flatten_exchange.py --confirm

Без --confirmationа — только отчёт.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, "/app/bot")
from dotenv import load_dotenv  # noqa: E402

parser = argparse.ArgumentParser()
parser.add_argument("--confirm", action="store_true", help="выполнить (без флага — отчёт)")
args = parser.parse_args()

BOT_ENV = (os.getenv("BOT_ENV") or "testnet").strip().lower() or "testnet"
for candidate in (f"/app/.env.{BOT_ENV}", "/app/.env"):
    if os.path.exists(candidate):
        load_dotenv(candidate, override=True)
        break
TESTNET = (os.getenv("BINANCE_TESTNET") or "false").strip().lower() == "true"

from binance import AsyncClient  # noqa: E402


async def collect_all(client, extra_symbols):
    """Все открытые позиции, обычные и algo-ордера по всем символам."""
    positions = await client.futures_position_information()
    opened = [p for p in positions if float(p.get("positionAmt", 0) or 0) != 0]

    orders = []
    try:
        # В новых версиях API /fapi/v1/openOrders возвращает все символы,
        # если symbol не передан.
        orders = await client.futures_get_open_orders()
        print(f"  обычные ордера одним запросом: {len(orders)}")
    except Exception as e:
        print(f"  (все обычные ордера одним запросом не получились: {e})")

    algos = []
    try:
        algos = await client.futures_get_all_algo_orders()
        print(f"  algo-ордера одним запросом: {len(algos)}")
    except Exception as e:
        print(f"  (все algo одним запросом не получились: {e})")

    # Символы из конфигов ботов: там могут висеть TP-ордера без позиции
    # (позицию закрыли, TP забыли) — их тоже нужно снять.
    symbols = ({p["symbol"] for p in opened}
               | {o["symbol"] for o in orders}
               | {a["symbol"] for a in algos}
               | set(extra_symbols))
    for sym in sorted(symbols):
        if not any(o["symbol"] == sym for o in orders):
            try:
                orders.extend(await client.futures_get_open_orders(symbol=sym))
            except Exception:
                pass
        if not any(a["symbol"] == sym for a in algos):
            try:
                algos.extend(await client.futures_get_open_algo_orders(symbol=sym))
            except Exception:
                pass
    return opened, orders, algos


def bot_symbols():
    """Символы из конфигов текущего окружения (bot/configs/<env>/*.yaml)."""
    import glob
    import re
    out = set()
    for path in glob.glob(f"/app/bot/configs/{BOT_ENV}/config_*.yaml"):
        m = re.search(r"^symbol:\s*([A-Z0-9]+)\s*$", open(path, encoding="utf-8").read(), re.M)
        if m:
            out.add(m.group(1))
        else:  # имя файла как запасной вариант
            base = os.path.basename(path)[len("config_"):-len(".yaml")]
            out.add(base.upper() + "USDT")
    return out


async def main():
    client = await AsyncClient.create(
        api_key=os.environ["BINANCE_API_KEY"],
        api_secret=os.environ["BINANCE_API_SECRET"],
        testnet=TESTNET,
    )
    try:
        print(f"ОКРУЖЕНИЕ={BOT_ENV}  BINANCE_TESTNET={TESTNET}")
        opened, orders, algos = await collect_all(client, bot_symbols())
        print()
        print(f"  позиций с ненулевым объёмом: {len(opened)}")
        print(f"  открытых обычных ордеров:     {len(orders)}")
        print(f"  открытых algo-ордеров:        {len(algos)}")

        if not args.confirm:
            print("\nЭто отчёт. Повторите с --confirm для выполнения.")
            return

        # 1. Снимаем ордера ДО закрытия позиций: reduceOnly-конфликт не даст
        #    закрыть позицию, пока висит её лимитный ордер.
        cancelled = 0
        for o in orders:
            try:
                await client.futures_cancel_order(symbol=o["symbol"], orderId=o["orderId"])
                cancelled += 1
            except Exception as e:
                print(f"  ! не снят ордер {o['symbol']}/{o['orderId']}: {e}")
        for a in algos:
            try:
                await client.futures_cancel_algo_order(symbol=a["symbol"], algoId=a["algoId"])
                cancelled += 1
            except Exception as e:
                print(f"  ! не снят algo {a['symbol']}/{a.get('algoId')}: {e}")
        print(f"  снято ордеров: {cancelled}")

        # 2. Закрываем позиции рынком.
        closed = 0
        for p in opened:
            amt = float(p["positionAmt"])
            side = "SELL" if amt > 0 else "BUY"
            try:
                await client.futures_create_order(
                    symbol=p["symbol"], side=side, type="MARKET", quantity=abs(amt),
                )
                closed += 1
            except Exception as e:
                print(f"  ! не закрыта {p['symbol']}: {e}")
        print(f"  закрыто позиций: {closed}")

        # 3. Контрольный опрос.
        await asyncio.sleep(2)
        left_pos = [p for p in await client.futures_position_information()
                    if float(p.get("positionAmt", 0) or 0) != 0]
        left_ord = []
        left_algo = []
        for sym in sorted({p["symbol"] for p in left_pos}
                          | {a["symbol"] for a in algos}
                          | set(bot_symbols())):
            try:
                left_ord.extend(await client.futures_get_open_orders(symbol=sym))
            except Exception:
                pass
            try:
                left_algo.extend(await client.futures_get_open_algo_orders(symbol=sym))
            except Exception:
                pass
        print()
        print(f"ИТОГ: осталось позиций={len(left_pos)} ордеров={len(left_ord)} algo={len(left_algo)}")
        if left_pos or left_ord or left_algo:
            print("ВНИМАНИЕ: что-то осталось, проверьте вручную.")
            sys.exit(1)
        print("Биржа чистая.")
    finally:
        await client.close_connection()


asyncio.run(main())