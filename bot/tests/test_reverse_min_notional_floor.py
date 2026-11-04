"""Флор нотионала reverse-ноги (bot/order_manager.py).

Позицию меньше биржевого MIN_NOTIONAL нельзя ни защитить TP-лимитом
(-2022 «ReduceOnly Order is rejected»), ни закрыть рынком (-4164). В
$10-режиме хедж часто выходил на $2–4 и «зависал» без выхода (LINKUSDT 03.11:
нога 0.37 ≈ $4.09). Поэтому итоговое нетто поднимается до минимума: цена цели
T от размера ноги не зависит, цикл на той же цели даёт даже больший профит.

Запуск: cd bot && python -m pytest tests/test_reverse_min_notional_floor.py -q
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from order_manager import OrderManager, _round_step_up  # noqa: E402

LOG = logging.getLogger("test-rev-floor")


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    """Клиент не нужен: все сетевые методы подменяются в тесте."""

    def __init__(self):
        self.calls = []


def make_om(min_notional=5.0, step=0.01):
    cfg = SimpleNamespace(
        symbol="LINKUSDT", mode="live", taker_fee_pct=0.05,
        reverse_profit_pct=0.5, reverse_breakeven_pct=0.5,
        reverse_fee_buffer_pct=1.0, tp1_close_pct=100,
        exchange_sl_backstop_enabled=True,
    )
    om = OrderManager(cfg, LOG, client=FakeClient())
    om._step_size = step
    om._tick_size = 0.001
    om._price_precision = 3
    om._min_notional = min_notional
    om._max_qty = 1_000_000.0
    om._market_max_qty = 1_000_000.0
    return om


def patch_io(om, sizing):
    """Подменяет сеть/сайзинг, оставляя логику флора настоящей."""
    calls = []

    om._reverse_sizing = lambda *a, **k: sizing

    async def fake_split(side, qty, mode, fallback_price=0.0):
        calls.append(("ORDER", side, qty))
        return qty, fallback_price, 1

    async def fake_tp(direction, price, qty):
        calls.append(("TP", direction, price, qty))

    async def no_cap():
        return 0.0

    async def no_lev():
        return None

    om._place_market_split = fake_split
    om._place_tp_limit = fake_tp
    om._notional_cap_usd = no_cap
    om._set_leverage = no_lev
    return calls


def chain(om):
    return run(om._open_reverse_position_chain(
        original_direction="LONG",
        original_entry=11.128,
        original_qty=0.45,
        sl_price=11.042,
        mode="live",
        net_position=0.45,
        net_realized=0.0,
        step=0,
        unrealized=-0.039,
        position_entry=11.128,
    ))


# --- округление вверх ------------------------------------------------------

def test_round_step_up_ceil():
    assert _round_step_up(0.4619, 0.01) == pytest.approx(0.47)
    assert _round_step_up(0.46, 0.01) == pytest.approx(0.46)   # ровный шаг не растим
    assert _round_step_up(0.4601, 0.01) == pytest.approx(0.47)
    assert _round_step_up(5.0, 1.0) == pytest.approx(5.0)
    assert _round_step_up(5.1, 1.0) == pytest.approx(6.0)


# --- флор ноги -------------------------------------------------------------

def test_dust_reverse_leg_is_raised_to_min_notional():
    """LINKUSDT: сайзинг дал нетто 0.3747 ≈ $4.14 < $5 → нога растёт до $5+."""
    om = make_om()
    # (T, S_total, q, f_in, f_out, p, w) — как в реальном логе 03.11 20:14
    calls = patch_io(om, (10.866, -0.3747, -0.8247, 0.0005, 0.0005, 0.005, 0.016))

    res = chain(om)

    order = [c for c in calls if c[0] == "ORDER"]
    assert len(order) == 1
    _, side, qty = order[0]
    assert side == "SELL"                       # закрываем LONG и уходим в SHORT
    assert qty == pytest.approx(0.92, abs=1e-9)  # q = 0.47 + 0.45
    # нога 0.47 * 11.042 = $5.19 >= minNotional $5
    assert 0.47 * 11.042 >= 5.0

    tp = [c for c in calls if c[0] == "TP"]
    assert len(tp) == 1
    assert tp[0][1] == "SHORT"
    assert tp[0][2] == pytest.approx(10.866, abs=1e-6)
    assert tp[0][3] == pytest.approx(0.47, abs=1e-9)

    assert res is not None
    entry, held, tp_price = res
    assert held == pytest.approx(0.47, abs=1e-9)
    assert tp_price == pytest.approx(10.866, abs=1e-6)


def test_leg_above_min_notional_is_not_changed():
    """Нога $6.63 — флор не срабатывает, сайзинг не трогаем."""
    om = make_om()
    calls = patch_io(om, (10.866, -0.60, -1.05, 0.0005, 0.0005, 0.005, 0.016))

    chain(om)

    order = [c for c in calls if c[0] == "ORDER"]
    assert len(order) == 1
    assert order[0][1] == "SELL"
    assert order[0][2] == pytest.approx(1.05, abs=1e-9)

    tp = [c for c in calls if c[0] == "TP"]
    assert tp[0][3] == pytest.approx(0.60, abs=1e-9)


def test_floor_disabled_when_min_notional_unknown():
    """minNotional неизвестен (0) — поведение прежнее, без подъёма ноги."""
    om = make_om(min_notional=0.0)
    calls = patch_io(om, (10.866, -0.3747, -0.8247, 0.0005, 0.0005, 0.005, 0.016))

    chain(om)

    order = [c for c in calls if c[0] == "ORDER"]
    assert order[0][2] == pytest.approx(0.82, abs=1e-9)  # 0.8247 → шаг 0.01 вниз
