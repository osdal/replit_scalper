"""Kill-switch: закрывает все открытые позиции на бирже и снимает ордера.

Запускается API-сервером (POST /api/live/close-all). Использует те же env-ключи,
что и бот (BOT_ENV: testnet/live).
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
    results = []
    try:
        positions = await client.futures_position_information()
        for p in positions:
            try:
                amt = float(p.get("positionAmt", 0) or 0)
            except (TypeError, ValueError):
                continue
            if abs(amt) < 1e-12:
                continue
            sym = p.get("symbol")
            side = "SELL" if amt > 0 else "BUY"
            qty = abs(amt)
            entry = {"symbol": sym, "positionAmt": amt, "side": side, "qty": qty}
            try:
                await client.futures_cancel_all_open_orders(symbol=sym)
            except Exception as e:  # noqa: BLE001
                entry["cancel_error"] = str(e)[:160]
            try:
                o = await client.futures_create_order(
                    symbol=sym, side=side, type="MARKET", quantity=qty, reduceOnly=True
                )
                entry["closed"] = True
                entry["orderId"] = o.get("orderId")
            except Exception as e:  # noqa: BLE001
                entry["close_error"] = str(e)[:160]
            results.append(entry)
    finally:
        await client.close_connection()
    print(json.dumps({"env": BOT_ENV, "testnet": TESTNET, "closed": len([r for r in results if r.get("closed")]), "results": results}, ensure_ascii=False))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
