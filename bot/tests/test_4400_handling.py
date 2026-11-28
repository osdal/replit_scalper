"""Тесты обработки -4400 как transient-лимита и guard на вход при живой позиции."""
import asyncio
import os
import sys

from binance.exceptions import BinanceAPIException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rate_limit  # noqa: E402
import order_manager as om  # noqa: E402


def test_4400_is_rate_limit_not_ban():
    exc = BinanceAPIException(None, 400, '{"code":-4400,"msg":"only reduceOnly"}')
    assert rate_limit.is_rate_limit_error(exc) is True
    assert rate_limit.is_ban_error(exc) is False
    assert -4400 in rate_limit._RATE_LIMIT_CODES


def test_exchange_position_qty_guard():
    class Fake:
        async def _get_real_position_qty(self):
            return 0.01

    class Empty:
        async def _get_real_position_qty(self):
            return 0.0

    class Broken:
        async def _get_real_position_qty(self):
            raise RuntimeError("api down")

    assert asyncio.run(om._exchange_position_qty(Fake())) == 0.01
    assert asyncio.run(om._exchange_position_qty(Empty())) == 0.0
    assert asyncio.run(om._exchange_position_qty(Broken())) == 0.0
