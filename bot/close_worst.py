"""Закрывает ОДНУ самую убыточную открытую позицию (по нереализованному PnL).

Запускается API-сервером при срабатывании лимита просадки (см. routes/live.ts).
Использует те же env-ключи, что и бот (BOT_ENV: testnet/live).
Печатает JSON с результатом: какой символ был самым убыточным и закрылся ли он.
"""
import asyncio
import json
import os
import sys

from binance import AsyncClient

_this = os.path.dirname(os.path.abspath(__file__))
BOT_ENV = (os.getenv("BOT_ENV") or "testnet").strip().lower() or "testnet"


def _load_env() -> None:
    try:
        from dotenv import load_dotenv
    except Exception:
        return
    load_dotenv(os.path.join(_this, "..", f".env.{BOT_ENV}"), override=True)
    load_dotenv(os.path.join(_this, "..", ".env"), override=False)
    load_dotenv(override=False)


_load_env()
TESTNET = (os.getenv("BINANCE_TESTNET", "false").lower() == "true")


async def main() -> None:
    client = await AsyncClient.create(
        api_key=os.getenv("BINANCE_API_KEY") or None,
        api_secret=os.getenv("BINANCE_API_SECRET") or None,
        testnet=TESTNET,
    )
    # Опциональный аргумент: закрыть КОНКРЕТНЫЙ символ (а не «худшую» позицию).
    target = (sys.argv[1] if len(sys.argv) > 1 else "").strip().upper()
    worst = None
    try:
        positions = await client.futures_position_information()
        for p in positions:
            sym = p.get("symbol")
            if target and sym != target:
                continue
            try:
                amt = float(p.get("positionAmt", 0) or 0)
            except (TypeError, ValueError):
                continue
            if abs(amt) < 1e-12:
                continue
            try:
                upnl = float(p.get("unRealizedProfit", 0) or 0)
            except (TypeError, ValueError):
                upnl = 0.0
            if worst is None or upnl < worst["upnl"]:
                worst = {"symbol": sym, "amt": amt, "upnl": upnl}
    finally:
        if worst is None:
            await client.close_connection()
            print(json.dumps({"env": BOT_ENV, "closed": 0, "reason": "no open positions"}))
            return

    # Не закрываем прибыльные позиции: если худшая не в минусе — ничего не делаем.
    # (Иначе при dd>=порога закрывались бы и плюсовые позиции — лишний черн/комиссии.)
    if worst["upnl"] >= 0:
        await client.close_connection()
        print(json.dumps({"env": BOT_ENV, "closed": 0, "reason": "no losing position",
                          "worst_symbol": worst["symbol"], "worst_upnl": worst["upnl"]}))
        return

    amt = worst["amt"]
    sym = worst["symbol"]
    side = "SELL" if amt > 0 else "BUY"
    qty = abs(amt)
    result = {"env": BOT_ENV, "testnet": TESTNET, "symbol": sym, "upnl": worst["upnl"],
              "positionAmt": amt, "side": side, "qty": qty}
    try:
        await client.futures_cancel_all_open_orders(symbol=sym)
    except Exception as e:  # noqa: BLE001
        result["cancel_error"] = str(e)[:160]
    try:
        o = await client.futures_create_order(
            symbol=sym, side=side, type="MARKET", quantity=qty, reduceOnly=True
        )
        result["closed"] = True
        result["orderId"] = o.get("orderId")
    except Exception as e:  # noqa: BLE001
        result["closed"] = False
        result["close_error"] = str(e)[:200]
    await client.close_connection()
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
