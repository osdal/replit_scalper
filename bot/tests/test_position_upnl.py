"""Unrealized PnL из positionRisk: Binance отдаёт ключ `unRealizedProfit`.

Регрессия: раньше читался только `unrealizedProfit`, поэтому плавающий PnL
всегда был 0 — cum-loss cap и EXPIRED не видели текущий результат цикла.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from order_manager import _position_upnl  # noqa: E402


def test_reads_exchange_camel_case_key():
    assert _position_upnl({"unRealizedProfit": "-1.25"}) == -1.25
    assert _position_upnl({"unRealizedProfit": "0.734"}) == 0.734


def test_falls_back_to_legacy_lowercase_key():
    assert _position_upnl({"unrealizedProfit": "0.5"}) == 0.5


def test_missing_and_invalid_values_are_zero():
    assert _position_upnl({}) == 0.0
    assert _position_upnl({"unRealizedProfit": None}) == 0.0
    assert _position_upnl({"unRealizedProfit": "abc"}) == 0.0
