"""Приёмочные тесты ворот на НАСТОЯЩЕМ python-binance (поддельна только aiohttp-сессия).

Запуск:  cd bot && python -m pytest tests/test_binance_gate_real_client.py -q
Тесты не зависят от внутренностей реализации: смотрят, ушёл ли запрос в сеть и что увидел вызывающий код.
"""
import asyncio
import json
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from binance.async_client import AsyncClient  # noqa: E402
from binance.exceptions import BinanceAPIException  # noqa: E402
import binance_gate as bg  # noqa: E402


class FakeResp:
    def __init__(self, status, body, headers=None):
        self.status, self._b, self.headers = status, body, headers or {}

    async def text(self):
        return json.dumps(self._b)

    async def json(self):
        return self._b

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self):
        self.script, self.urls = [], []

    def _n(self, url):
        self.urls.append(str(url))
        return self.script.pop(0)

    def get(self, url, **kw):
        return self._n(url)

    def post(self, url, **kw):
        return self._n(url)

    def delete(self, url, **kw):
        return self._n(url)

    async def close(self):
        pass


def make():
    gate = bg.AccountGate(path=os.path.join(tempfile.mkdtemp(), "gate.json"))
    client = AsyncClient("k", "s")
    sess = FakeSession()
    client.session = sess
    bg.wrap_client(client, gate)
    return gate, client, sess


def run(c):
    return asyncio.run(c)


def ok(body=None, weight="1900"):
    return FakeResp(200, [] if body is None else body, {"X-MBX-USED-WEIGHT-1M": weight})


def test_ban_is_recorded_even_right_after_a_successful_response():
    gate, c, s = make()
    s.script = [ok(weight="100")]
    run(c.futures_position_information(symbol="BTCUSDT"))     # успешный запрос только что записал состояние
    ban = int(time.time() * 1000) + 120_000
    s.script = [FakeResp(418, {"code": -1003, "msg": f"Way too many requests; IP banned until {ban}."})]
    with pytest.raises(BinanceAPIException):
        run(c.futures_account_balance())
    assert gate.is_banned(), "бан, пришедший менее чем через секунду после успешного ответа, потерян"


def test_plain_http_429_pauses_further_requests():
    """429 без даты бана: Binance шлёт code=-1003 (не -429!). Нужна пауза, иначе боты дойдут до 418."""
    gate, c, s = make()
    s.script = [FakeResp(429, {"code": -1003, "msg": "Too many requests; current limit of IP is 2400 requests per minute."},
                         {"Retry-After": "20"})]
    with pytest.raises(BinanceAPIException):
        run(c.futures_account_balance())
    assert gate.is_banned(), "после 429 запросы не приостановлены"


@pytest.mark.parametrize("name,call", [
    ("klines прогрева limit=300", lambda c: c.futures_klines(symbol="BTCUSDT", interval="5m", limit=300)),
    ("статус ордера GET /order", lambda c: c.futures_get_order(symbol="BTCUSDT", orderId=1)),
    ("REST-цена ticker/price", lambda c: c.futures_symbol_ticker(symbol="BTCUSDT")),
    ("смена marginType при старте", lambda c: c.futures_change_margin_type(symbol="BTCUSDT", marginType="ISOLATED")),
    ("openOrders", lambda c: c.futures_get_open_orders(symbol="BTCUSDT")),
    ("позиции positionRisk", lambda c: c.futures_position_information(symbol="BTCUSDT")),
    ("плечо POST /leverage", lambda c: c.futures_change_leverage(symbol="BTCUSDT", leverage=5)),
])
def test_startup_and_protection_calls_are_not_shed_at_79_percent_weight(name, call):
    gate, c, s = make()
    gate.record_response(1900)                                  # 79% лимита, порог shed = 75%
    s.script = [ok({} if "ticker" in name or "статус" in name or "marginType" in name or "плечо" in name else [])]
    before = len(s.urls)
    try:
        run(call(c))
    except BinanceAPIException:
        pass
    assert len(s.urls) == before + 1, f"{name}: запрос отброшен воротами по весу"


def test_secondary_calls_are_still_shed_at_high_weight():
    gate, c, s = make()
    gate.record_response(1900)
    s.script = [ok()]
    before = len(s.urls)
    with pytest.raises(Exception):
        run(c.futures_klines(symbol="BTCUSDT", interval="5m", limit=2))   # опрос свечей — вторичный
    assert len(s.urls) == before


def test_block_looks_like_a_binance_error_to_existing_code():
    """with_retry и except BinanceAPIException в боте должны понимать блокировку ворот."""
    gate, c, s = make()
    ban = int(time.time() * 1000) + 120_000
    gate.record_ban(ban)
    with pytest.raises(BinanceAPIException) as ei:
        run(c.futures_account_balance())
    assert ei.value.code == -1003 and "banned until" in str(ei.value)       # with_retry -> wait_for_ban
    gate2, c2, s2 = make()
    gate2.record_response(2300)
    with pytest.raises(BinanceAPIException) as ei2:
        run(c2.futures_klines(symbol="BTCUSDT", interval="5m", limit=2))
    assert ei2.value.code == -429                                            # with_retry -> backoff и повтор
