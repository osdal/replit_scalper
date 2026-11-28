"""Тест chain_sim: синтетическая цепочка из 3 ног."""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import chain_sim as cs  # noqa: E402


def test_three_leg_chain():
    t0 = datetime(2026, 11, 11, tzinfo=timezone.utc)
    # open, high, low; SL первой ноги, TP разворота при отрицательном net, SL 2-го разворота
    rows = [(100, 101, 99), (100, 100.2, 99.4), (99.5, 99.6, 99.4),
            (97.9, 98.1, 97.8), (97.9, 98.0, 97.85), (97.5, 97.5, 97.3)]
    candles = [(t0 + timedelta(minutes=5*i), o, h, l, o)
               for i, (o, h, l) in enumerate(rows)]
    c = cs.simulate_cycle("TEST", candles, 0, "LONG", cs.load_cfg("ZECUSDT"), 1, "A")
    assert c.depth == 3
    assert c.reason == "REVERSE_BE"
    assert abs(c.net - (-0.190494)) < 1e-5
    assert abs(c.legs[0].qty - 0.109687) < 1e-6
    assert abs(c.legs[1].qty - 0.050251) < 1e-6
    assert abs(c.legs[2].qty - 0.11048) < 1e-6
    assert abs(c.legs[1].tp - 97.908) < 1e-6
    assert abs(c.legs[2].sl - 97.4105) < 1e-6
