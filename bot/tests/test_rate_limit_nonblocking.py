"""Бан -1003 не должен блокировать горячий путь (обработчик свечи, tick-цикл).

Блокирующий sleep внутри обработчика свечи останавливает чтение WS-сокета,
очередь переполняется (BinanceWebsocketQueueOverflow) и бот слепнет до конца
бана. Поэтому по умолчанию wait_for_ban только фиксирует окно бана.
"""
import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from binance.exceptions import BinanceAPIException  # noqa: E402
import rate_limit as rl  # noqa: E402


def _ban_exc(until_ms=None):
    if until_ms is None:
        until_ms = int((time.time() + 3600) * 1000)
    msg = f"Way too many requests; IP(1.2.3.4) banned until {until_ms}."
    return BinanceAPIException(None, 418, json.dumps({"code": -1003, "msg": msg}))


@pytest.fixture(autouse=True)
def _reset_state():
    rl.reset_ban_state()
    yield
    rl.reset_ban_state()


def test_ban_error_is_detected():
    assert rl.is_ban_error(_ban_exc())


def test_wait_for_ban_does_not_sleep_by_default():
    exc = _ban_exc()
    t0 = time.monotonic()
    ok = asyncio.run(rl.wait_for_ban(exc))
    elapsed = time.monotonic() - t0

    assert ok is True
    assert elapsed < 0.5, f"wait_for_ban уснул на {elapsed:.2f}s — горячий путь заблокирован"
    # Окно бана зафиксировано: дальше запросы отсекает AccountGate.
    assert rl._active_ban_until_wall > time.time()


def test_repeated_same_ban_also_returns_fast():
    exc = _ban_exc()
    asyncio.run(rl.wait_for_ban(exc))

    t0 = time.monotonic()
    asyncio.run(rl.wait_for_ban(exc))
    assert time.monotonic() - t0 < 0.5


def test_block_mode_still_waits(monkeypatch):
    """Холодный путь может явно запросить прежнее поведение (block=True)."""
    monkeypatch.setattr(rl, "BAN_JITTER_MIN_SEC", 0.0)
    monkeypatch.setattr(rl, "BAN_JITTER_MAX_SEC", 0.0)
    until_ms = int((time.time() + 0.3) * 1000)

    t0 = time.monotonic()
    asyncio.run(rl.wait_for_ban(_ban_exc(until_ms), block=True))
    assert time.monotonic() - t0 >= 0.25


def test_with_retry_raises_immediately_on_ban():
    calls = {"n": 0}

    async def scenario():
        async def call():
            calls["n"] += 1
            raise _ban_exc()

        with pytest.raises(BinanceAPIException):
            await rl.with_retry(call)

    t0 = time.monotonic()
    asyncio.run(scenario())

    assert calls["n"] == 1, "при бане запрос не должен повторяться в том же вызове"
    assert time.monotonic() - t0 < 0.5
