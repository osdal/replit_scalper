#!/usr/bin/env python3
"""Юнит-тесты реконсилера PnL (bot/reconcile_pnl.py).

Запуск из корня проекта:

    python3 -m unittest discover -s tests -v

Только stdlib: pytest на сервере не установлен, а сам модуль импортируется без
binance (импорт Client — внутри main()).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "bot"))

import reconcile_pnl as R  # noqa: E402


def fill(ts, side, qty, price=1.0, rp=0.0, fee=0.0, asset="USDT"):
    return {
        "time": ts,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "realizedPnl": str(rp),
        "commission": str(fee),
        "commissionAsset": asset,
    }


def trips(fills):
    return R._round_trips(fills)


class ParseMsTest(unittest.TestCase):
    def test_naive_is_utc(self):
        self.assertEqual(R._parse_ms("2026-11-17T15:45:00"),
                         R._parse_ms("2026-11-17T15:45:00+00:00"))

    def test_z_suffix(self):
        self.assertEqual(R._parse_ms("2026-11-17T15:45:00Z"),
                         R._parse_ms("2026-11-17T17:45:00+02:00"))

    def test_microseconds(self):
        ms = R._parse_ms("2026-11-17T21:01:29.543000")
        self.assertEqual(ms % 1000, 543)


class RoundTripsTest(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(trips([]), [])

    def test_simple_long(self):
        out = trips([
            fill(1000, "BUY", 1, 100, rp=0, fee=0.05),
            fill(2000, "SELL", 1, 110, rp=10, fee=0.055),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["direction"], "LONG")
        self.assertEqual(out[0]["start"], 1000)
        self.assertEqual(out[0]["end"], 2000)
        self.assertAlmostEqual(out[0]["realized"], 10.0)
        # Комиссия из userTrades положительная, в net — со знаком минус.
        self.assertAlmostEqual(out[0]["commission"], -0.105)

    def test_partial_close_stays_one_trip(self):
        out = trips([
            fill(1000, "BUY", 10, 1, rp=0),
            fill(2000, "SELL", 4, 1.1, rp=0.4),
            fill(3000, "SELL", 6, 1.2, rp=1.2),
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["start"], 1000)
        self.assertEqual(out[0]["end"], 3000)
        self.assertAlmostEqual(out[0]["realized"], 1.6)

    def test_flip_closes_and_opens(self):
        out = trips([
            fill(1000, "BUY", 1, 100, rp=0),
            fill(2000, "SELL", 3, 110, rp=10),   # +1 закрывает лонг, -2 открывает шорт
            fill(3000, "BUY", 2, 100, rp=20),
        ])
        self.assertEqual(len(out), 2)
        self.assertEqual([t["direction"] for t in out], ["LONG", "SHORT"])
        self.assertEqual(out[0]["end"], 2000)
        self.assertEqual(out[1]["start"], 2000)
        self.assertEqual(out[1]["end"], 3000)

    def test_float_residue_is_flat_not_flip(self):
        """Регресс #48714 ATOMUSDT: -3.84 + 3.84 = 6.7e-16.

        Раньше это считалось переворотом в LONG, и на стыке циклов появлялась
        фантомная нога (0 PnL, только комиссия), из-за чего len(cycle) не
        совпадал с chain_depth и строка уходила в skip.
        """
        out = trips([
            fill(1_000, "BUY", 5.61, 1.336, rp=0, fee=0.00374748),
            fill(2_000, "SELL", 9.45, 1.330, rp=-0.03366, fee=0.00628425),
            fill(3_000, "BUY", 3.84, 1.312, rp=0.06912, fee=0.00251904),
            fill(4_000, "SELL", 5.75, 1.302, rp=0, fee=0.00374325),
            fill(5_000, "BUY", 4.20, 1.312, rp=-0.042, fee=0.00275520),
            fill(5_000, "BUY", 3.90, 1.312, rp=-0.0155, fee=0.00255840),
            fill(5_000, "BUY", 2.13, 1.312, rp=0, fee=0.00139728),
            fill(6_000, "SELL", 4.48, 1.331, rp=0.08512, fee=0.00298144),
        ])
        self.assertEqual(len(out), 4, [(t["direction"], t["start"], t["end"]) for t in out])
        self.assertFalse(
            any(t["start"] == 3_000 for t in out),
            "фантомная нога на стыке цикла не должна появляться",
        )
        # Цикл #48714: шорт 15:50 -> 15:58 и лонг 15:58 -> 21:01.
        cycle = [t for t in out if t["start"] >= 4_000]
        self.assertEqual(len(cycle), 2)
        self.assertEqual(cycle[0]["direction"], "SHORT")
        self.assertEqual(cycle[1]["direction"], "LONG")
        net = sum(t["realized"] for t in cycle) + sum(t["commission"] for t in cycle)
        self.assertAlmostEqual(net, 0.014184, places=6)

    def test_non_usdt_commission_ignored(self):
        out = trips([
            fill(1000, "BUY", 1, 100, rp=0, fee=0.001, asset="BNB"),
            fill(2000, "SELL", 1, 110, rp=10, fee=0.001, asset="USDT"),
        ])
        self.assertAlmostEqual(out[0]["commission"], -0.001)


class AlignFlatTest(unittest.TestCase):
    def test_drops_leading_closing_fill(self):
        """Хвост прошлого цикла: закрывающий филл попал в выборку."""
        fills = [
            fill(1000, "SELL", 2, 100, rp=5.0),   # хвост прошлого цикла
            fill(2000, "BUY", 2, 101, rp=0),
            fill(3000, "SELL", 2, 102, rp=2.0),
        ]
        out = R._align_flat(fills)
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0]["time"], 2000)

    def test_keeps_series_after_flat(self):
        fills = [
            fill(1000, "BUY", 1, 100, rp=0),
            fill(2000, "SELL", 1, 101, rp=1.0),
            fill(3000, "BUY", 1, 102, rp=0),
            fill(4000, "SELL", 1, 103, rp=1.0),
        ]
        self.assertEqual(len(R._align_flat(fills)), 4)

    def test_empty(self):
        self.assertEqual(R._align_flat([]), [])


if __name__ == "__main__":
    unittest.main()
