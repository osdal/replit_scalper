"""Закрытие «протухшей» reverse-позиции в минимальный плюс (bot/main.py).

Правило: позиция старше MAX_POSITIONS_IGNORE_AFTER_HOURS больше не занимает слот
max_positions, поэтому её нельзя держать «до своего TP» бесконечно. Как только
суммарный net цикла (реализованный + плавающий) покрывает комиссию выхода и
EXPIRED_CLOSE_MIN_PROFIT_USD — закрываем рынком.

Запуск:  cd bot && python -m pytest tests/test_expired_profit_close.py -q
"""
import asyncio
import logging
import os
import sys
import time
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

LOG = logging.getLogger("test-expired-profit")
HOUR_MS = 3_600_000


def run(coro):
    return asyncio.run(coro)


class FakeTracker:
    def __init__(self, pos, entry_ms):
        self.position = pos
        self._trade_id = 123
        self._entry_ms = entry_ms

    async def _entry_time_ms(self, trade_id, prefer_db=False):
        return self._entry_ms


class FakeOrderMgr:
    def __init__(self):
        self.cfg = SimpleNamespace(mode="live", taker_fee_pct=0.05)


class FakeCap:
    def __init__(self, total_pnl):
        self.total_pnl = total_pnl
        self.calls = 0

    async def evaluate(self, symbol, order_mgr, tracker, log):
        self.calls += 1
        return SimpleNamespace(
            total_pnl=self.total_pnl,
            realized_pnl=self.total_pnl - 0.5,
            unrealized_pnl=0.5,
        )


def _pos(is_reverse=True, qty=4.0, entry=6.948, mode="live"):
    return SimpleNamespace(
        is_reverse=is_reverse, mode=mode, direction="LONG",
        remaining_qty=qty, entry_price=entry,
    )


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(main, "_EXPIRED_CLOSE_HOURS", 2.0)
    monkeypatch.setattr(main, "_EXPIRED_CLOSE_MIN_PROFIT_USD", 0.0)
    monkeypatch.setattr(main, "_EXPIRED_CLOSE_INTERVAL_SEC", 10.0)
    monkeypatch.setattr(main, "_expired_close_ts", {})


def _age_ms(hours):
    return int(time.time() * 1000) - int(hours * HOUR_MS)


# комиссия выхода для qty=4, entry=6.948 при taker 0.05% ≈ 0.0139
EXIT_FEE = 4.0 * 6.948 * 0.0005


def test_young_cycle_is_not_touched(env, monkeypatch):
    cap = FakeCap(total_pnl=1.0)
    monkeypatch.setattr(main, "_CUM_CAP", cap)
    tracker = FakeTracker(_pos(), _age_ms(1.0))
    assert run(main._expired_cycle_profit_snapshot("UNIUSDT", FakeOrderMgr(), tracker, LOG)) is None
    assert cap.calls == 0


def test_expired_cycle_in_minimal_profit_is_ready(env, monkeypatch):
    cap = FakeCap(total_pnl=EXIT_FEE + 0.01)
    monkeypatch.setattr(main, "_CUM_CAP", cap)
    tracker = FakeTracker(_pos(), _age_ms(3.0))
    snap = run(main._expired_cycle_profit_snapshot("UNIUSDT", FakeOrderMgr(), tracker, LOG))
    assert snap is not None
    assert snap.total_pnl == pytest.approx(EXIT_FEE + 0.01)
    assert cap.calls == 1


def test_expired_but_profit_below_exit_fee_waits(env, monkeypatch):
    """«Плюс» меньше комиссии выхода рынком — после закрытия был бы минус."""
    cap = FakeCap(total_pnl=EXIT_FEE * 0.5)
    monkeypatch.setattr(main, "_CUM_CAP", cap)
    tracker = FakeTracker(_pos(), _age_ms(3.0))
    assert run(main._expired_cycle_profit_snapshot("UNIUSDT", FakeOrderMgr(), tracker, LOG)) is None


def test_min_profit_buffer_is_added_to_threshold(env, monkeypatch):
    monkeypatch.setattr(main, "_EXPIRED_CLOSE_MIN_PROFIT_USD", 0.05)
    cap = FakeCap(total_pnl=EXIT_FEE + 0.01)
    monkeypatch.setattr(main, "_CUM_CAP", cap)
    tracker = FakeTracker(_pos(), _age_ms(3.0))
    assert run(main._expired_cycle_profit_snapshot("UNIUSDT", FakeOrderMgr(), tracker, LOG)) is None
    cap.total_pnl = EXIT_FEE + 0.05 + 0.001
    main._expired_close_ts.clear()  # снять троттлинг между проверками в тесте
    assert run(main._expired_cycle_profit_snapshot("UNIUSDT", FakeOrderMgr(), tracker, LOG)) is not None


def test_disabled_when_hours_zero(env, monkeypatch):
    monkeypatch.setattr(main, "_EXPIRED_CLOSE_HOURS", 0.0)
    cap = FakeCap(total_pnl=5.0)
    monkeypatch.setattr(main, "_CUM_CAP", cap)
    tracker = FakeTracker(_pos(), _age_ms(100.0))
    assert run(main._expired_cycle_profit_snapshot("UNIUSDT", FakeOrderMgr(), tracker, LOG)) is None
    assert cap.calls == 0


def test_ignores_non_reverse_and_paper(env, monkeypatch):
    cap = FakeCap(total_pnl=5.0)
    monkeypatch.setattr(main, "_CUM_CAP", cap)
    plain = FakeTracker(_pos(is_reverse=False), _age_ms(100.0))
    paper = FakeTracker(_pos(mode="paper"), _age_ms(100.0))
    assert run(main._expired_cycle_profit_snapshot("UNIUSDT", FakeOrderMgr(), plain, LOG)) is None
    assert run(main._expired_cycle_profit_snapshot("UNIUSDT", FakeOrderMgr(), paper, LOG)) is None
    assert cap.calls == 0


def test_throttled_between_ticks(env, monkeypatch):
    cap = FakeCap(total_pnl=EXIT_FEE + 0.01)
    monkeypatch.setattr(main, "_CUM_CAP", cap)
    tracker = FakeTracker(_pos(), _age_ms(3.0))
    assert run(main._expired_cycle_profit_snapshot("UNIUSDT", FakeOrderMgr(), tracker, LOG)) is not None
    assert run(main._expired_cycle_profit_snapshot("UNIUSDT", FakeOrderMgr(), tracker, LOG)) is None
    assert cap.calls == 1


def test_missing_entry_time_skips(env, monkeypatch):
    cap = FakeCap(total_pnl=5.0)
    monkeypatch.setattr(main, "_CUM_CAP", cap)
    tracker = FakeTracker(_pos(), 0)
    assert run(main._expired_cycle_profit_snapshot("UNIUSDT", FakeOrderMgr(), tracker, LOG)) is None
    assert cap.calls == 0
