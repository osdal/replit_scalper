"""Тесты cumulative loss cap (bot/cum_loss_cap.py).

Запуск:  cd bot && python -m pytest tests/test_cum_loss_cap.py -q
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cum_loss_cap import CumLossCap  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeTracker:
    """Минимальная заглушка PositionTracker."""

    def __init__(self, net_realized=0.0, step=0, is_reverse=True, open_=True):
        self.net_realized = net_realized
        self.calls = 0
        self.raise_exc = None
        self.position = SimpleNamespace(is_reverse=is_reverse, reverse_chain_step=step)
        self._open = open_

    def has_open_position(self):
        return self._open

    async def cycle_realized_net(self):
        self.calls += 1
        if self.raise_exc:
            raise self.raise_exc
        if self.net_realized is None:
            return None, 0.0, 0.0
        return self.net_realized, self.net_realized, 0.0


class FakeOrderMgr:
    def __init__(self, wallet=40.0, upnl=0.0):
        self.wallet = wallet
        self.upnl = upnl
        self.upnl_calls = 0
        self.balance_calls = 0
        self.balance_raises = False

    async def get_unrealized_pnl(self, fresh=False):
        self.upnl_calls += 1
        return self.upnl

    async def get_balance(self, mode=None, force=False):
        self.balance_calls += 1
        if self.balance_raises:
            raise RuntimeError("boom")
        return self.wallet


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


@pytest.fixture
def log():
    return logging.getLogger("test-cum-cap")


def make_cap(**kw):
    clk = Clock()
    kw.setdefault("pct", 5.0)
    kw.setdefault("interval_sec", 1.0)
    return CumLossCap(clock=clk, **kw), clk


# --------------------------------------------------------------------------
# Пример пользователя: депозит $40, 5% = $2.00
#   ноги: -0.1, -0.3, -1.0 (realized = -1.4), 4-я нога плавает до -0.6 → cap
# --------------------------------------------------------------------------
def test_user_example_triggers_exactly_at_5pct(log):
    cap, _ = make_cap()
    # wallet после трёх закрытых ног: 40 - 0.1 - 0.3 - 1.0 = 38.6
    om = FakeOrderMgr(wallet=38.6)
    tr = FakeTracker(net_realized=-(0.1 + 0.3 + 1.0), step=3)

    for upnl, expect_hit in [(-0.1, False), (-0.4, False), (-0.59, False),
                             (-0.6, True), (-0.75, True)]:
        om.upnl = upnl
        snap = run(cap.evaluate("XRPUSDT", om, tr, log))
        assert snap is not None
        assert snap.ref == pytest.approx(40.0), "депозит цикла = 40, а не текущий баланс 38.6"
        assert snap.threshold == pytest.approx(2.0)
        assert snap.hit is expect_hit, (upnl, snap.describe())


def test_threshold_not_shrinking_with_wallet(log):
    """Старый баг: база = текущий баланс → порог $1.93 вместо $2.00."""
    cap, _ = make_cap()
    om = FakeOrderMgr(wallet=38.6)
    tr = FakeTracker(net_realized=-1.4, step=3)
    om.upnl = -0.55  # итого -1.95: при плавающей базе (1.93) сработало бы, при фиксированной — нет
    snap = run(cap.evaluate("XRPUSDT", om, tr, log))
    assert snap.cum_loss == pytest.approx(1.95)
    assert not snap.hit


def test_ref_fixed_once_per_cycle(log):
    cap, _ = make_cap()
    om = FakeOrderMgr(wallet=39.9)
    tr = FakeTracker(net_realized=-0.1, step=1)
    s1 = run(cap.evaluate("XRPUSDT", om, tr, log))
    assert s1.ref == pytest.approx(40.0)
    # нога закрылась ещё в минус: баланс и net изменились — ref остаётся 40
    om.wallet, tr.net_realized, tr.position.reverse_chain_step = 39.6, -0.4, 2
    s2 = run(cap.evaluate("XRPUSDT", om, tr, log))
    assert s2.ref == pytest.approx(40.0)
    assert om.balance_calls == 1
    # новый цикл → reset → ref берётся заново
    cap.reset("XRPUSDT")
    om.wallet, tr.net_realized = 50.0, 0.0
    s3 = run(cap.evaluate("XRPUSDT", om, tr, log))
    assert s3.ref == pytest.approx(50.0)


def test_fixed_reference_deposit_overrides_wallet(log):
    cap, _ = make_cap(fixed_ref_usd=40.0)
    om = FakeOrderMgr(wallet=100.0)
    tr = FakeTracker(net_realized=-1.4)
    om.upnl = -0.6
    snap = run(cap.evaluate("XRPUSDT", om, tr, log))
    assert snap.threshold == pytest.approx(2.0) and snap.hit
    assert om.balance_calls == 0


def test_float_noise_does_not_miss_exact_threshold(log):
    cap, _ = make_cap()
    om = FakeOrderMgr(wallet=40 - (0.1 + 0.3 + 1.0))
    tr = FakeTracker(net_realized=-(0.1 + 0.3 + 1.0))
    om.upnl = -0.6
    assert run(cap.evaluate("X", om, tr, log)).hit


def test_profits_offset_losses_signed_sum(log):
    cap, _ = make_cap()
    om = FakeOrderMgr(wallet=38.0)
    # реализовано -2.1, но текущая нога в плюсе +0.3 → итог -1.8 < 2.0
    tr = FakeTracker(net_realized=-2.1)
    om.upnl = +0.3
    snap = run(cap.evaluate("X", om, tr, log))
    assert snap.cum_loss == pytest.approx(1.8) and not snap.hit
    # реализовано в плюсе +0.5, плавающий -2.4 → итог -1.9 < 2.0
    tr2, om2 = FakeTracker(net_realized=+0.5), FakeOrderMgr(wallet=40.5)
    om2.upnl = -2.4
    cap2, _ = make_cap()
    s2 = run(cap2.evaluate("X", om2, tr2, log))
    assert s2.cum_loss == pytest.approx(1.9) and not s2.hit


# --------------------------------------------------------------------------
# Сбои данных: не «убытка нет», а «проверка пропущена» + предупреждение
# --------------------------------------------------------------------------
def test_realized_unavailable_returns_none_and_warns(log, caplog):
    cap, clk = make_cap()
    om = FakeOrderMgr()
    tr = FakeTracker(net_realized=None)
    with caplog.at_level(logging.WARNING, logger="test-cum-cap"):
        assert run(cap.evaluate("X", om, tr, log)) is None
        assert run(cap.evaluate("X", om, tr, log)) is None  # внутри окна — без спама
        assert len([r for r in caplog.records if "skipped" in r.message]) == 1
        clk.t += 61
        assert run(cap.evaluate("X", om, tr, log)) is None
        assert len([r for r in caplog.records if "skipped" in r.message]) == 2


def test_realized_exception_returns_none(log, caplog):
    cap, _ = make_cap()
    tr = FakeTracker()
    tr.raise_exc = RuntimeError("api down")
    with caplog.at_level(logging.WARNING, logger="test-cum-cap"):
        assert run(cap.evaluate("X", FakeOrderMgr(), tr, log)) is None
    assert any("realized unavailable" in r.message for r in caplog.records)


def test_unrealized_unavailable_returns_none(log, caplog):
    cap, _ = make_cap()
    om = FakeOrderMgr()
    om.upnl = None
    with caplog.at_level(logging.WARNING, logger="test-cum-cap"):
        assert run(cap.evaluate("X", om, FakeTracker(net_realized=-5.0), log)) is None
    assert any("unrealized unavailable" in r.message for r in caplog.records)


def test_balance_failure_returns_none(log):
    cap, _ = make_cap()
    om = FakeOrderMgr()
    om.balance_raises = True
    assert run(cap.evaluate("X", om, FakeTracker(net_realized=-1.0), log)) is None


# --------------------------------------------------------------------------
# should_close: троттлинг, кэш realized, не-reverse позиции, выключено
# --------------------------------------------------------------------------
def test_should_close_throttled_by_interval(log):
    cap, clk = make_cap(interval_sec=1.0)
    om = FakeOrderMgr(wallet=38.6, upnl=-0.1)
    tr = FakeTracker(net_realized=-1.4, step=3)
    assert run(cap.should_close("X", om, tr, log)) is None
    assert om.upnl_calls == 1
    om.upnl = -0.7
    assert run(cap.should_close("X", om, tr, log)) is None   # <1с — не опрашиваем
    assert om.upnl_calls == 1
    clk.t += 1.0
    snap = run(cap.should_close("X", om, tr, log))           # прошла секунда — сработал
    assert snap is not None and snap.hit and om.upnl_calls == 2


def test_realized_cached_per_step_and_refreshed(log):
    cap, clk = make_cap(interval_sec=0.0, realized_ttl_sec=30.0)
    om = FakeOrderMgr(wallet=38.6, upnl=-0.1)
    tr = FakeTracker(net_realized=-1.4, step=3)
    for _ in range(5):
        run(cap.evaluate("X", om, tr, log))
    assert tr.calls == 1                                   # тяжёлый запрос — один раз
    run(cap.evaluate("X", om, tr, log, fresh=True))
    assert tr.calls == 2                                   # fresh обходит кэш
    tr.position.reverse_chain_step = 4
    run(cap.evaluate("X", om, tr, log))
    assert tr.calls == 3                                   # новая нога → пересчёт
    clk.t += 31
    run(cap.evaluate("X", om, tr, log))
    assert tr.calls == 4                                   # TTL истёк


def test_non_reverse_or_closed_position_resets_and_skips(log):
    cap, _ = make_cap()
    om = FakeOrderMgr(wallet=38.6, upnl=-9.0)
    assert run(cap.should_close("X", om, FakeTracker(is_reverse=False), log)) is None
    assert run(cap.should_close("X", om, FakeTracker(open_=False), log)) is None
    assert om.upnl_calls == 0


def test_disabled_when_pct_zero(log):
    cap, _ = make_cap(pct=0.0)
    assert not cap.enabled
    om = FakeOrderMgr(wallet=1.0, upnl=-100.0)
    assert run(cap.should_close("X", om, FakeTracker(net_realized=-50.0), log)) is None


def test_precomputed_values_skip_rest_calls(log):
    """Путь «перед открытием ноги»: realized/unrealized уже известны caller'у."""
    cap, _ = make_cap()
    om = FakeOrderMgr(wallet=38.6)
    tr = FakeTracker(net_realized=999.0)
    snap = run(cap.evaluate("X", om, tr, log, fresh=True, net_realized=-1.4, unrealized=-0.6))
    assert snap.hit and tr.calls == 0 and om.upnl_calls == 0


# --------------------------------------------------------------------------
# Метки выхода в трекере
# --------------------------------------------------------------------------
def test_tracker_labels_for_cum_loss_cap():
    # logger.py открывает файловые хендлеры при импорте — нужна папка bot/logs.
    os.makedirs(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs"),
                exist_ok=True)
    pt = pytest.importorskip("position_tracker")
    stub = SimpleNamespace()
    cls = pt.PositionTracker
    assert cls._classify_reverse_exit(stub, None, 1.0, "SL", closed_by="cum_loss_cap") \
        == "REVERSE_CUM_LOSS_CAP"
    assert cls._reverse_close_reason(stub, "cum_loss_cap") == "cum_loss_cap"
    # прежние метки не сломаны
    assert cls._classify_reverse_exit(stub, None, 1.0, "SL", closed_by="chain_stop") \
        == "REVERSE_CHAIN_STOP"
    assert cls._reverse_close_reason(stub, "chain_stop") == "chain_max"
