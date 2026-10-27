"""Cap после неудавшегося SL (bot/main.py:_cap_after_unclosed_sl).

Сценарий: на виртуальном SL реверс-нога не открылась (max_positions на сервере,
слот занят свежим символом, отказ биржи). Позиция жива, каждый следующий тик
снова даёт hit="SL", а тиковая проверка cap выполняется только при `not hit` —
без этой доборки цикл уезжал бы до биржевого backstop вместо закрытия на
разрешённом минусе.

Запуск:  cd bot && python -m pytest tests/test_cap_after_unclosed_sl.py -q
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

LOG = logging.getLogger("test-cap-after-sl")


def run(coro):
    return asyncio.run(coro)


class FakeTracker:
    def __init__(self, pos):
        self.position = pos


def _pos(is_reverse=True):
    return SimpleNamespace(is_reverse=is_reverse, direction="LONG")


class CapStub:
    """Заглушка `_check_reverse_cum_loss`: считает вызовы и отдаёт заданный ответ."""

    def __init__(self, result=False, raise_exc=None):
        self.result = result
        self.raise_exc = raise_exc
        self.calls = []

    async def __call__(self, symbol, order_mgr, tracker, log):
        self.calls.append(symbol)
        if self.raise_exc:
            raise self.raise_exc
        return self.result


@pytest.fixture
def cap_enabled(monkeypatch):
    monkeypatch.setattr(main, "_CUM_CAP", SimpleNamespace(enabled=True, interval_sec=1.0))


def test_returns_true_when_cap_hits_after_failed_sl(cap_enabled, monkeypatch):
    stub = CapStub(result=True)
    monkeypatch.setattr(main, "_check_reverse_cum_loss", stub)
    tracker = FakeTracker(_pos())
    assert run(main._cap_after_unclosed_sl("SL", "UNIUSDT", object(), tracker, LOG)) is True
    assert stub.calls == ["UNIUSDT"]


def test_returns_false_when_cap_not_hit(cap_enabled, monkeypatch):
    stub = CapStub(result=False)
    monkeypatch.setattr(main, "_check_reverse_cum_loss", stub)
    assert run(main._cap_after_unclosed_sl("SL", "UNIUSDT", object(), FakeTracker(_pos()), LOG)) is False
    assert stub.calls == ["UNIUSDT"]


@pytest.mark.parametrize("hit", [None, "TP1", "TP2", "CUM_LOSS_CAP", ""])
def test_ignores_non_sl_hits(cap_enabled, monkeypatch, hit):
    stub = CapStub(result=True)
    monkeypatch.setattr(main, "_check_reverse_cum_loss", stub)
    assert run(main._cap_after_unclosed_sl(hit, "UNIUSDT", object(), FakeTracker(_pos()), LOG)) is False
    assert stub.calls == []


def test_ignores_non_reverse_and_closed_position(cap_enabled, monkeypatch):
    stub = CapStub(result=True)
    monkeypatch.setattr(main, "_check_reverse_cum_loss", stub)
    tracker = FakeTracker(_pos(is_reverse=False))
    assert run(main._cap_after_unclosed_sl("SL", "UNIUSDT", object(), tracker, LOG)) is False
    assert run(main._cap_after_unclosed_sl("SL", "UNIUSDT", object(), FakeTracker(None), LOG)) is False
    assert stub.calls == []


def test_disabled_cap_is_noop(monkeypatch):
    monkeypatch.setattr(main, "_CUM_CAP", SimpleNamespace(enabled=False, interval_sec=1.0))
    stub = CapStub(result=True)
    monkeypatch.setattr(main, "_check_reverse_cum_loss", stub)
    assert run(main._cap_after_unclosed_sl("SL", "UNIUSDT", object(), FakeTracker(_pos()), LOG)) is False
    assert stub.calls == []


def test_cap_error_does_not_break_tick(cap_enabled, monkeypatch):
    stub = CapStub(raise_exc=RuntimeError("boom"))
    monkeypatch.setattr(main, "_check_reverse_cum_loss", stub)
    assert run(main._cap_after_unclosed_sl("SL", "UNIUSDT", object(), FakeTracker(_pos()), LOG)) is False
    assert stub.calls == ["UNIUSDT"]
