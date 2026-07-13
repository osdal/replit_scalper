"""
Запускает бэктест с параметрами, переданными через stdin как JSON.
Вызывается из API сервера (artifacts/api-server/src/routes/backtest.ts)
вместо генерации Python-кода строкой — безопаснее и проще читать.

Формат входных данных (stdin, JSON):
{
  "symbol": "ETHUSDT",
  "start": "2026-05-01",
  "end": "2026-06-01",
  "config": { "timeframe": "5m", "leverage": 10, ... }
}

Вывод (stdout, JSON): результаты бэктеста или {"error": "..."}
"""
import asyncio
import json
import logging
import os
import sys

from binance import AsyncClient
from dotenv import load_dotenv

from config import Config
from backtester import run_backtest

load_dotenv()


async def main() -> None:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as e:
        print(json.dumps({"error": f"Invalid JSON input: {e}"}))
        return

    symbol = payload.get("symbol")
    start = payload.get("start")
    end = payload.get("end")
    config_overrides = payload.get("config", {})

    if not symbol or not start or not end:
        print(json.dumps({"error": "symbol, start, end are required"}))
        return

    try:
        cfg = Config(
            symbol=symbol,
            mode="backtest",
            backtest_start=start,
            backtest_end=end,
            log_file="logs/backtest.log",
            **config_overrides,
        )
    except (TypeError, ValueError) as e:
        # TypeError — неизвестное поле или отсутствует обязательное
        # ValueError — провалена валидация в Config.__post_init__
        print(json.dumps({"error": f"Invalid config: {e}"}))
        return

    client = await AsyncClient.create(
        api_key=os.getenv("BINANCE_API_KEY") or None,
        api_secret=os.getenv("BINANCE_API_SECRET") or None,
    )
    try:
        stats = await run_backtest(cfg, client, logging.getLogger("backtest_runner"))
        result = {
            "total_trades": stats.total_trades,
            "wins": stats.wins,
            "losses": stats.losses,
            "win_rate": round(stats.win_rate, 1),
            "total_pnl": round(stats.total_pnl, 4),
            "avg_win": round(stats.avg_win, 4),
            "avg_loss": round(stats.avg_loss, 4),
            "max_drawdown": round(stats.max_drawdown, 2),
            "initial_balance": stats.initial_balance,
            "final_balance": round(stats.final_balance, 2),
            "return_pct": round(
                (stats.final_balance - stats.initial_balance) / stats.initial_balance * 100, 2
            ),
        }
        print(json.dumps(result))
    except Exception as e:
        print(json.dumps({"error": str(e)}))
    finally:
        await client.close_connection()


if __name__ == "__main__":
    asyncio.run(main())
