"""Публичный контракт CumLossCap, который использует bot/main.py, и состояние цепочки.

Регрессия: методы mark_chain_ended/is_in_chain/_warn были случайно вынесены за класс
(потеряли отступ), из-за чего evaluate/should_close исчезали из класса, а ошибка
AttributeError глоталась на уровне debug в тиковом цикле — cap молча не работал.
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from cum_loss_cap import CumLossCap  # noqa: E402

LOG = logging.getLogger("chain-state")


def run(c):
    return asyncio.run(c)


class Tracker:
    def __init__(self, is_reverse=True, open_=True, net=-1.4, step=1):
        self.position = SimpleNamespace(is_reverse=is_reverse, reverse_chain_step=step)
        self._open = open_
        self.net = net

    def has_open_position(self):
        return self._open

    async def cycle_realized_net(self):
        return self.net, self.net, 0.0


class OM:
    def __init__(self, wallet=38.6, upnl=-0.1):
        self.wallet, self.upnl = wallet, upnl

    async def get_unrealized_pnl(self, fresh=False):
        return self.upnl

    async def get_balance(self, mode=None, force=False):
        return self.wallet


def test_public_api_used_by_main_is_on_the_class():
    for name in ("evaluate", "should_close", "reset", "mark_chain_ended", "is_in_chain", "_warn", "enabled"):
        assert hasattr(CumLossCap, name), f"CumLossCap.{name} отсутствует (методы вне класса?)"


def test_fixed_reference_deposit_survives_second_evaluate():
    """REVERSE_CUM_REF_DEPOSIT_USD>0: второй вызов не должен падать KeyError."""
    cap = CumLossCap(pct=5.0, fixed_ref_usd=40.0, interval_sec=0, min_interval_sec=0)
    tr, om = Tracker(), OM(wallet=100.0, upnl=-0.6)
    s1 = run(cap.evaluate("X", om, tr, LOG))
    s2 = run(cap.evaluate("X", om, tr, LOG))
    assert s1.ref == s2.ref == 40.0
    assert s2.threshold == 2.0 and s2.hit


def test_ref_stays_fixed_across_legs_and_resets_on_flat():
    cap = CumLossCap(pct=5.0, interval_sec=0, min_interval_sec=0)
    tr, om = Tracker(net=-0.1), OM(wallet=39.9)
    assert run(cap.evaluate("X", om, tr, LOG)).ref == 40.0
    tr.net, tr.position.reverse_chain_step, om.wallet = -1.4, 3, 38.6
    assert run(cap.evaluate("X", om, tr, LOG)).ref == 40.0   # не «уехал» вниз
    assert cap.is_in_chain("X")
    # позиции больше нет → should_close завершает цепочку и сбрасывает депозит цикла
    tr._open = False
    assert run(cap.should_close("X", om, tr, LOG)) is None
    assert not cap.is_in_chain("X")
    tr._open, tr.net, om.wallet = True, 0.0, 50.0
    assert run(cap.evaluate("X", om, tr, LOG)).ref == 50.0   # новый цикл — новая база


def test_mark_chain_ended_then_reset():
    cap = CumLossCap(pct=5.0, interval_sec=0, min_interval_sec=0)
    run(cap.evaluate("X", OM(), Tracker(), LOG))
    assert cap.is_in_chain("X")
    cap.mark_chain_ended("X")
    assert not cap.is_in_chain("X")
    cap.reset("X")
    assert run(cap.evaluate("X", OM(wallet=10.0), Tracker(net=0.0), LOG)).ref == 10.0
