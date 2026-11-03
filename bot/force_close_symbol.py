"""Принудительно закрывает указанные символы рынком (cancel + reduceOnly market).

В отличие от close_worst.py закрывает позицию независимо от знака PnL —
нужно для ручной разгрузки старых легаси-позиций (например, после смены
режима размера), когда штатное автозакрытие «только в плюс» не срабатывает.

Запуск (в контейнере api-testnet):
    BOT_ENV=testnet python3 force_close_symbol.py ATOMUSDT DOGEUSDT
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


async def _position_qty(client, symbol: str) -> float:
    positions = await client.futures_position_information(symbol=symbol)
    return sum(float(p.get("positionAmt", 0) or 0) for p in positions)


async def _cancel_algo_orders(client, symbol: str) -> int:
    """Снимает условные (algo) ордера: TP/SL closePosition и т.п."""
    cancelled = 0
    try:
        algos = await client.futures_get_open_algo_orders(symbol=symbol)
    except Exception:
        return 0
    for a in algos or []:
        algo_id = a.get("algoId")
        if not algo_id:
            continue
        try:
            # Подпись DELETE-запроса — в query string (как в order_manager).
            await client._request_futures_api(
                "delete", "algoOrder", True,
                force_params=True, data={"algoId": int(algo_id)},
            )
            cancelled += 1
        except Exception:
            pass
    return cancelled


async def close_symbol(client, symbol: str) -> dict:
    res: dict = {"symbol": symbol}
    try:
        amt = await _position_qty(client, symbol)
    except Exception as e:  # noqa: BLE001
        res["error"] = f"position fetch failed: {e}"
        return res
    res["positionAmt"] = amt
    if abs(amt) < 1e-12:
        res["closed"] = False
        res["reason"] = "no position"
        return res
    try:
        await client.futures_cancel_all_open_orders(symbol=symbol)
    except Exception as e:  # noqa: BLE001
        res["cancel_error"] = str(e)[:160]
    res["algo_cancelled"] = await _cancel_algo_orders(client, symbol)
    side = "SELL" if amt > 0 else "BUY"
    qty = abs(amt)
    try:
        order = await client.futures_create_order(
            symbol=symbol, side=side, type="MARKET",
            quantity=qty, reduceOnly=True,
        )
        res["closed"] = True
        res["orderId"] = order.get("orderId")
        res["side"] = side
        res["qty"] = qty
    except Exception as e:  # noqa: BLE001
        res["closed"] = False
        res["close_error"] = str(e)[:200]
    return res


async def main() -> None:
    symbols = [s.strip().upper() for s in sys.argv[1:] if s.strip()]
    if not symbols:
        print(json.dumps({"error": "symbols required"}))
        sys.exit(2)
    client = await AsyncClient.create(
        api_key=os.getenv("BINANCE_API_KEY") or None,
        api_secret=os.getenv("BINANCE_API_SECRET") or None,
        testnet=TESTNET,
    )
    try:
        results = [await close_symbol(client, s) for s in symbols]
    finally:
        await client.close_connection()
    print(json.dumps(
        {"env": BOT_ENV, "testnet": TESTNET, "results": results},
        ensure_ascii=False,
    ))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
