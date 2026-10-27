"""Пересчёт TP reverse-цепи под фактический объём (bot/main.py).

Сценарий: цепочка сайзилась на больший объём, чем реально встал (потолок
нотионала / ручной reduceOnly), TP остался от планового объёма и закрывал цикл
в минус. `_reverse_cycle_tp_price` даёт уровень, на котором цикл выходит ровно
в ноль + `reverse_profit_pct`.

Запуск:  cd bot && python -m pytest tests/test_reverse_tp_recalc.py -q
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

LOG = logging.getLogger("test-reverse-tp")


def run(coro):
    return asyncio.run(coro)


def _cycle_pnl(direction, qty, entry, tp, net_realized, fee_pct):
    """Фактический результат цикла на TP: net закрытых ног + нога − комиссия выхода."""
    s = 1.0 if direction == "LONG" else -1.0
    return net_realized + s * qty * (tp - entry) - (fee_pct / 100.0) * qty * tp


# --- чистая формула -------------------------------------------------------

def test_long_tp_matches_uni_cycle():
    """UNIUSDT: LONG 4.0 @ 6.948, реализованный net цикла −3.700102."""
    tp = main._reverse_cycle_tp_price("LONG", 4.0, 6.948, -3.700102, 0.05, 0.1)
    assert tp == pytest.approx(7.8849, abs=2e-4)
    # На этом TP цикл выходит в целевую прибыль (0.1% notional), не в минус.
    pnl = _cycle_pnl("LONG", 4.0, 6.948, tp, -3.700102, 0.05)
    assert pnl == pytest.approx(0.001 * 4.0 * tp, abs=1e-9)


def test_short_tp_stays_below_entry_and_reaches_target():
    tp = main._reverse_cycle_tp_price("SHORT", 4.0, 7.0, -3.7, 0.05, 0.1)
    assert tp < 7.0
    assert tp == pytest.approx(6.0659, abs=5e-4)
    pnl = _cycle_pnl("SHORT", 4.0, 7.0, tp, -3.7, 0.05)
    assert pnl == pytest.approx(0.001 * 4.0 * tp, abs=1e-9)


def test_breakeven_without_profit():
    """profit_pct=0 → ровно безубыток цикла (с комиссией выхода)."""
    tp = main._reverse_cycle_tp_price("LONG", 4.0, 6.948, -3.700102, 0.05, 0.0)
    pnl = _cycle_pnl("LONG", 4.0, 6.948, tp, -3.700102, 0.05)
    assert pnl == pytest.approx(0.0, abs=1e-9)


def test_invalid_inputs_return_zero():
    assert main._reverse_cycle_tp_price("LONG", 0.0, 6.948, -3.7, 0.05, 0.1) == 0.0
    assert main._reverse_cycle_tp_price("LONG", 4.0, 0.0, -3.7, 0.05, 0.1) == 0.0
    assert main._reverse_cycle_tp_price("LONG", None, 6.948, -3.7, 0.05, 0.1) == 0.0


# --- пересчёт с биржей (заглушки) -----------------------------------------

class FakeClient:
    def __init__(self, orders):
        self.orders = list(orders)
        self.cancelled = []
        self.placed = []

    async def futures_get_open_orders(self, symbol):
        return list(self.orders)

    async def futures_cancel_order(self, symbol, orderId):
        self.cancelled.append(orderId)

    async def futures_create_order(self, **kwargs):
        self.placed.append(kwargs)


class FakeOrderMgr:
    _tick_size = 0.001
    exchange_tp_price = 0.0

    def __init__(self, qty=4.0, orders=None):
        self.qty = qty
        self.cfg = SimpleNamespace(
            symbol="UNIUSDT", mode="live",
            taker_fee_pct=0.05, reverse_profit_pct=0.1,
        )
        self.client = FakeClient(orders or [])

    async def _get_real_position_qty(self, direction):
        return self.qty

    async def _adjust_price(self, price, mode="live"):
        return round(round(price / 0.001) * 0.001, 12)

    async def _place_tp_limit(self, direction, price, qty):
        self.exchange_tp_price = price
        self.client.placed.append({"direction": direction, "price": price, "qty": qty})


class FakeTracker:
    def __init__(self, pos, net_realized=-3.700102):
        self.position = pos
        self.net_realized = net_realized
        self.saved = 0

    async def cycle_realized_net(self):
        return self.net_realized, self.net_realized, 0.0

    def _save_state(self):
        self.saved += 1


def _uni_pos(tp=7.225):
    return SimpleNamespace(
        is_reverse=True, mode="live", direction="LONG",
        entry_price=6.948, tp1_price=tp, tp2_price=tp,
        total_qty=4.0, remaining_qty=4.0, tp1_hit=False,
        exchange_tp_price=0.0,
    )


def test_recalc_replaces_stale_tp_for_actual_qty():
    pos = _uni_pos(tp=7.225)
    tracker = FakeTracker(pos)
    om = FakeOrderMgr(qty=4.0, orders=[
        {"type": "LIMIT", "side": "SELL", "reduceOnly": True, "orderId": 488878601},
        {"type": "STOP_MARKET", "side": "SELL", "reduceOnly": True, "orderId": 999},
    ])
    changed = run(main._recalc_reverse_cycle_tp(om, tracker, pos, LOG))
    assert changed is True
    assert om.client.cancelled == [488878601]          # только старый TP-лимит
    assert om.client.placed == [{"direction": "LONG", "price": 7.885, "qty": 4.0}]
    assert pos.tp1_price == pos.tp2_price == 7.885
    assert pos.exchange_tp_price == 7.885
    assert tracker.saved == 1


def test_recalc_is_idempotent_when_tp_already_correct():
    pos = _uni_pos(tp=7.885)
    tracker = FakeTracker(pos)
    om = FakeOrderMgr(qty=4.0, orders=[
        {"type": "LIMIT", "side": "SELL", "reduceOnly": True, "orderId": 1},
    ])
    changed = run(main._recalc_reverse_cycle_tp(om, tracker, pos, LOG))
    assert changed is False
    assert om.client.cancelled == []
    assert om.client.placed == []


def test_recalc_skips_non_reverse_position():
    pos = _uni_pos(tp=7.225)
    pos.is_reverse = False
    tracker = FakeTracker(pos)
    om = FakeOrderMgr(qty=4.0)
    changed = run(main._recalc_reverse_cycle_tp(om, tracker, pos, LOG))
    assert changed is False
    assert om.client.placed == []
