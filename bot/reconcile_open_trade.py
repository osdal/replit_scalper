"""Закрывает «фантомную» открытую строку trades по факту закрытия позиции.

Нужно, когда позиция уже закрыта (штатно, скриптом force_close_symbol.py или
вручную), а строка в БД осталась is_open=1: startup-синк такие строки старше
48 часов намеренно не трогает, и они навсегда занимают слот в лимитах
(max_positions_with_expired) и портят статистику.

PnL берётся из Binance за окно [entry_time, --until] (по умолчанию now):
net = realizedPnl + commission + funding (как в order_manager.get_realized_pnl).

Запуск (в контейнере api-testnet):
    BOT_ENV=testnet python3 reconcile_open_trade.py 162969 configs/testnet/config_doge.yaml \
        --until 2026-11-03T11:06:00
"""
import asyncio
import datetime
import json
import logging
import os
import sys
from typing import Optional

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


def _parse_until(value: Optional[str]) -> Optional[int]:
    """ISO-время (UTC) -> epoch ms. None, если не задано."""
    if not value:
        return None
    v = value.strip().replace("Z", "+00:00")
    dt = datetime.datetime.fromisoformat(v)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


def _entry_ms(row: dict) -> int:
    raw = (row or {}).get("entry_time")
    if not raw:
        return 0
    dt = datetime.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


async def _close_stats(client, symbol: str, entry_ms: int, exit_ms: int, direction: str) -> dict:
    """VWAP закрывающих филлов и суммарная комиссия за окно."""
    close_side = "SELL" if direction == "LONG" else "BUY"
    out = {"exit_price": None, "commission": 0.0, "fills": 0, "close_qty": 0.0}
    try:
        fills = await client.futures_account_trades(
            symbol=symbol, startTime=entry_ms, endTime=exit_ms + 60000,
        )
    except Exception as e:  # noqa: BLE001
        out["error"] = str(e)[:200]
        return out
    notional = 0.0
    for f in fills or []:
        try:
            qty = float(f.get("qty", 0) or 0)
            price = float(f.get("price", 0) or 0)
            out["commission"] += float(f.get("commission", 0) or 0)
            out["fills"] += 1
            if (f.get("side") or "") == close_side:
                notional += qty * price
                out["close_qty"] += qty
        except (TypeError, ValueError):
            continue
    if out["close_qty"] > 0:
        out["exit_price"] = round(notional / out["close_qty"], 10)
    out["commission"] = round(out["commission"], 8)
    out["close_qty"] = round(out["close_qty"], 8)
    return out


async def main() -> None:
    args = [a for a in sys.argv[1:]]
    until_raw: Optional[str] = None
    if "--until" in args:
        i = args.index("--until")
        until_raw = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
    if len(args) < 2:
        print(json.dumps({"error": "usage: reconcile_open_trade.py <trade_id> <config_path> [--until ISO]"}))
        sys.exit(2)
    trade_id = int(args[0])
    config_path = args[1]

    cfg = load_config(config_path)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger(f"reconcile.{cfg.symbol}")

    client = await AsyncClient.create(
        api_key=os.getenv("BINANCE_API_KEY") or None,
        api_secret=os.getenv("BINANCE_API_SECRET") or None,
        testnet=TESTNET,
    )
    reporter = DbReporter(symbol=cfg.symbol, logger=log)
    result: dict = {"env": BOT_ENV, "testnet": TESTNET, "trade_id": trade_id, "symbol": cfg.symbol}
    try:
        row = await reporter.get_trade(trade_id)
        if not row:
            result["error"] = "trade row not found"
            print(json.dumps(result, ensure_ascii=False))
            return
        is_open = row.get("is_open")
        if isinstance(is_open, str):
            is_open = is_open.strip().lower() not in ("", "0", "false", "no")
        if not is_open:
            result["error"] = "trade row already closed"
            print(json.dumps(result, ensure_ascii=False))
            return

        entry_ms = _entry_ms(row)
        until_ms = _parse_until(until_raw) or int(datetime.datetime.now(
            datetime.timezone.utc).timestamp() * 1000)
        if entry_ms <= 0 or until_ms <= entry_ms:
            result["error"] = f"bad window entry_ms={entry_ms} until_ms={until_ms}"
            print(json.dumps(result, ensure_ascii=False))
            return

        exchange_amt = sum(
            float(p.get("positionAmt", 0) or 0)
            for p in await client.futures_position_information(symbol=cfg.symbol)
        )
        result["exchange_position_amt"] = exchange_amt
        if abs(exchange_amt) > 1e-9 and not until_raw:
            result["error"] = (
                "exchange still has an open position; pass --until <close time> "
                "if that position belongs to a newer cycle"
            )
            print(json.dumps(result, ensure_ascii=False))
            return

        direction = (row.get("direction") or "LONG").upper()
        stats = await _close_stats(client, cfg.symbol, entry_ms, until_ms, direction)

        order_mgr = OrderManager(cfg, log, client=client)
        pnl = await order_mgr.get_realized_pnl(cfg.symbol, entry_ms, until_ms)
        if pnl is None:
            pnl = 0.0

        exit_dt = datetime.datetime.fromtimestamp(until_ms / 1000.0, datetime.timezone.utc)
        patch = {
            "is_open": False,
            "status": "closed",
            "exit_reason": "reconcile_flat",
            "pnl": round(float(pnl), 4),
            "exit_time": exit_dt.isoformat(),
            "cycle_pnl": round(float(pnl), 4),
        }
        if stats.get("exit_price"):
            patch["exit_price"] = stats["exit_price"]
        if stats.get("commission"):
            patch["commission"] = stats["commission"]
        ok = await reporter.patch_trade(trade_id, patch)
        result.update({
            "closed": bool(ok),
            "entry_time": row.get("entry_time"),
            "until": exit_dt.isoformat(),
            "pnl": round(float(pnl), 4),
            "stats": stats,
        })
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
