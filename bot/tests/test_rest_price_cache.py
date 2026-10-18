"""Тесты Задание B: REST-кэш цены (market_data.get_current_price).

Запуск: cd bot && python -m pytest tests/test_rest_price_cache.py -q
"""
import asyncio
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from market_data import get_current_price, _rest_price_cache, _MIN_REST_INTERVAL_SEC  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeTicker:
    def __init__(self, price):
        self.price = price
    def get(self, key, default=0):
        return self.price if key == "price" else default


class FakeClient:
    def __init__(self, price=50000.0, should_fail=False):
        self.price = price
        self.should_fail = should_fail
        self.call_count = 0

    async def futures_symbol_ticker(self, symbol):
        self.call_count += 1
        if self.should_fail:
            raise Exception("network error")
        return {"price": self.price}


def test_cache_returns_cached_within_5s():
    """В течение 5с после первого REST-запроса возвращается кэш, новый запрос не делается."""
    _rest_price_cache.clear()
    client = FakeClient(price=50000.0)
    p1 = run(get_current_price(client, "BTCUSDT"))
    assert p1 == 50000.0
    assert client.call_count == 1
    # Второй вызов в течение 5с — из кэша, без нового запроса
    p2 = run(get_current_price(client, "BTCUSDT"))
    assert p2 == 50000.0
    assert client.call_count == 1  # не увеличился


def test_cache_expires_after_5s():
    """После 5с кэш истекает, следующий запрос идет в REST."""
    _rest_price_cache.clear()
    client = FakeClient(price=50000.0)
    p1 = run(get_current_price(client, "BTCUSDT"))
    assert p1 == 50000.0
    assert client.call_count == 1
    # Истекший кэш — эмулируем время, меняем цену в клиенте
    client.price = 51000.0
    cached = _rest_price_cache.get("BTCUSDT")
    cached = (cached[0], cached[1] - 10.0)  # 10 секунд назад
    _rest_price_cache["BTCUSDT"] = cached
    p2 = run(get_current_price(client, "BTCUSDT"))
    assert p2 == 51000.0
    assert client.call_count == 2


def test_min_interval_between_rest_requests():
    """Мин интервал 5с между REST-запросами одного бота к одному символу."""
    _rest_price_cache.clear()
    client = FakeClient(price=50000.0)
    p1 = run(get_current_price(client, "BTCUSDT"))
    assert p1 == 50000.0
    assert client.call_count == 1
    # Сразу второй вызов — кэш ещё живой (0 < 5с), запрос не делается
    p2 = run(get_current_price(client, "BTCUSDT"))
    assert p2 == 50000.0
    assert client.call_count == 1


def test_rest_failure_never_raises():
    """При ошибке REST возвращается последняя известная цена (или 0.0), не исключение."""
    _rest_price_cache.clear()
    client = FakeClient(price=0.0, should_fail=True)
    p = run(get_current_price(client, "BTCUSDT"))
    assert p == 0.0  # нет кэша, нет WS-цены → 0.0, но не исключение


def test_different_symbols_independent_cache():
    """Кэш для разных символов независимый."""
    _rest_price_cache.clear()
    client = FakeClient(price=100.0)
    p1 = run(get_current_price(client, "BTCUSDT"))
    assert p1 == 100.0
    # Разный символ — кэш не используется
    client.price = 200.0
    p2 = run(get_current_price(client, "ETHUSDT"))
    assert p2 == 200.0
    assert client.call_count == 2


def test_rest_failure_records_attempt_time():
    """При ошибке REST время попытки запоминается, и следующие 5с запросов не делается."""
    _rest_price_cache.clear()
    client = FakeClient(price=0.0, should_fail=True)
    p1 = run(get_current_price(client, "BTCUSDT"))
    assert p1 == 0.0
    assert client.call_count == 1
    # Сразу второй вызов — кэш попытки ещё жив (0 < 5с), запрос не делается
    p2 = run(get_current_price(client, "BTCUSDT"))
    assert p2 == 0.0
    assert client.call_count == 1  # не увеличился, т.к. кэш попытки ещё живой
