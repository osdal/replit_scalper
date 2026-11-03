"""Закрывает «фантомную» открытую строку trades по факту флэта на бирже.

Нужно, когда позиция уже закрыта (штатно, скриптом force_close_symbol.py или
вручную), а строка в БД осталась is_open=1: startup-синк такие строки старше
48 часов намеренно не трогает, и они навсегда занимают слот в лимитах
(max_positions_with_expired) и портят статистику.

Скрипт берёт реальные филлы из userTrades за окно [entry_time, now], считает
net-PnL и комиссию и закрывает строку тем же путём, что и бот
(PositionTracker.close_open_trade_from_exchange).

Запуск (в контейнере api-testnet):
    BOT_ENV=testnet python3 reconcile_open_trade.py 162969 configs/testnet/config_doge.yaml
"""
import asyncio
import json
import logging
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

from config import load_config          # noqa: E402
from db_reporter import DbReporter      # noqa: E402
from order_manager import OrderManager  # noqa: E402
from position_tracker import PositionTracker  # noqa: E402


async def main() -> None:
    if len(sys.argv) < 3:
        print(json.dumps({"error": "usage: reconcile_open_trade.py <trade_id> <config_path>"}))
        sys.exit(2)
    trade_id = int(sys.argv[1])
    config_path = sys.argv[2]

    cfg = load_config(config_path)
    log = logging.getLogger(f"reconcile.{cfg.symbol}")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    client = await AsyncClient.create(
        api_key=os.getenv("BINANCE_API_KEY") or None,
        api_secret=os.getenv("BINANCE_API_SECRET") or None,
        testnet=TESTNET,
    )
    reporter = DbReporter(symbol=cfg.symbol, logger=log)
    result: dict = {"env": BOT_ENV, "testnet": TESTNET, "trade_id": trade_id, "symbol": cfg.symbol}
    try:
        row = await reporter.get_trade(trade_id)
        result["row_before"] = {
            k: row.get(k) for k in ("symbol", "direction", "qty", "entry_price", "is_open", "status", "pnl")
        } if row else None

        order_mgr = OrderManager(cfg, log, client=client)
        tracker = PositionTracker(cfg, log, reporter=reporter, order_mgr=order_mgr, notifier=None)

        # Убеждаемся, что на бирже действительно нет позиции — иначе закрывать
        # строку нельзя: она отражает реальный риск.
        positions = await client.futures_position_information(symbol=cfg.symbol)
        amt = sum(float(p.get("positionAmt", 0) or 0) for p in positions)
        result["exchange_position_amt"] = amt
        if abs(amt) > 1e-9:
            result["error"] = "exchange still has an open position — refusing to close DB row"
            print(json.dumps(result, ensure_ascii=False))
            return

        tracker._trade_id = trade_id
        ok = await tracker.close_open_trade_from_exchange(
            exit_reason="reconcile_flat",
            direction=(row or {}).get("direction") or "LONG",
        )
        result["closed"] = bool(ok)
        row_after = await reporter.get_trade(trade_id)
        result["row_after"] = {
            k: row_after.get(k) for k in ("is_open", "status", "pnl", "exit_reason", "exit_price", "exit_time")
        } if row_after else None
    finally:
        await reporter.close()
        await client.close_connection()

    print(json.dumps(result, ensure_ascii=False, default=str))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"error": str(e)}))
        sys.exit(1)
