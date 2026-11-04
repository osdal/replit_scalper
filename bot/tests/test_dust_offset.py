"""Неттинг dust-остатка при входе (bot/order_manager.py::open_position).

Пыль меньше MIN_NOTIONAL нельзя ни защитить TP (-2022), ни закрыть рынком
(-4164). Раньше новый вход складывался с ней: SUIUSDT 04.11 — dust 3.6 +
новая нога 5.5 = 9.1 на бирже при двух строках в БД. Теперь ордер шлётся на
разницу (план − dust), а TP/SL и учёт ведутся по полному размеру позиции.
Если новая часть сама меньше minNotional — вход пропускается.

Запуск: cd bot && python -m pytest tests/test_dust_offset.py -q
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from order_manager import OrderManager  # noqa: E402

LOG = logging.getLogger("test-dust-offset")


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    def __init__(self):
        self.orders = []

    async def futures_create_order(self, **kw):
        self.orders.append(kw)
        return {"orderId": len(self.orders), "status": "FILLED"}


def make_om(client, fixed_qty=2.0, min_notional=5.0):
    cfg = SimpleNamespace(
        symbol="SUIUSDT", mode="live", leverage=10,
        position_size_usd=0.0, position_size_pct=0.0, margin_pct=0.0,
        fixed_notional_usd=0.0, fixed_qty=fixed_qty, fixed_risk_usd=0.0,
        risk_pct=1.0, sl_pct=0.5, tp1_pct=1.0, tp1_close_pct=100,
        use_fixed_tp_sl=True,
    )
    om = OrderManager(cfg, LOG, client=client)
    om._step_size = 0.01
    om._tick_size = 0.001
    om._price_precision = 3
    om._min_notional = min_notional
    om._max_qty = 1_000_000.0
    om._market_max_qty = 1_000_000.0

    async def balance(mode=None, force=False):
        return 100.0

    async def no_cap():
        return 0.0

    async def no_lev():
        return None

    async def fill(order, fallback):
        return fallback

    async def real_qty(direction):
        return state["positions"]

    async def real_entry(direction):
        return state["entry"]

    placed = {}

    async def place_all_orders(**kw):
        placed.update(kw)

    om.get_balance = balance
    om._notional_cap_usd = no_cap
    om._set_leverage = no_lev
    om._get_fill_price = fill
    om._get_real_position_qty = real_qty
    om._get_real_position_entry = real_entry
    om._place_all_orders = place_all_orders

    state = {"positions": 0.0, "entry": 10.0}
    return om, state, placed


def sig(direction="LONG", entry=10.0):
    return SimpleNamespace(
        direction=direction, entry_price=entry,
        sl_price=entry * 0.995, tp1_price=entry * 1.01,
        tp2_price=entry * 1.02,
    )


# --- основное: вычитание пыли в ту же сторону ------------------------------

def test_same_direction_dust_is_subtracted_from_new_order():
    client = FakeClient()
    om, state, placed = make_om(client, fixed_qty=2.0)
    state["positions"] = 2.0          # на бирже dust 0.5 + новая часть 1.5
    state["entry"] = 9.9              # средняя цена входа биржи

    res = run(om.open_position(sig(), dust_offset_qty=0.5))

    assert len(client.orders) == 1
    assert client.orders[0]["quantity"] == pytest.approx(1.5)
    assert res is not None
    entry, qty = res
    assert qty == pytest.approx(2.0)          # учёт по полной позиции
    assert entry == pytest.approx(9.9)        # средняя цена биржи
    assert placed["total_qty"] == pytest.approx(2.0)  # TP/SL на всю позицию


def test_opposite_direction_dust_is_added_to_new_order():
    """Пыль в противоположную сторону закрывается новым ордером — итог = план."""
    client = FakeClient()
    om, state, placed = make_om(client, fixed_qty=2.0)
    state["positions"] = 2.0          # dust 0.5 SHORT закрыт, открыт LONG 2.0
    state["entry"] = 10.0

    res = run(om.open_position(sig("LONG"), dust_offset_qty=-0.5))

    assert client.orders[0]["quantity"] == pytest.approx(2.5)
    assert res is not None and res[1] == pytest.approx(2.0)
    assert placed["total_qty"] == pytest.approx(2.0)


def test_no_dust_keeps_original_order_size():
    client = FakeClient()
    om, state, _ = make_om(client, fixed_qty=2.0)
    state["positions"] = 2.0

    res = run(om.open_position(sig()))

    assert client.orders[0]["quantity"] == pytest.approx(2.0)
    assert res is not None and res[1] == pytest.approx(2.0)


# --- пропуск входа ---------------------------------------------------------

def test_remainder_below_min_notional_skips_entry():
    """План $6, dust $4 → новая часть $2 < $5: вход не открываем."""
    client = FakeClient()
    om, state, _ = make_om(client, fixed_qty=0.6)
    state["positions"] = 0.6

    res = run(om.open_position(sig(entry=10.0), dust_offset_qty=0.4))

    assert res is None
    assert client.orders == []


def test_dust_already_covers_planned_size_skips_entry():
    client = FakeClient()
    om, state, _ = make_om(client, fixed_qty=0.5)
    state["positions"] = 0.5

    res = run(om.open_position(sig(entry=10.0), dust_offset_qty=0.6))

    assert res is None
    assert client.orders == []
