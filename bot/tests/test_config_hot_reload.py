"""Тесты горячего перечитывания конфига (bot/config.py).

Запуск (stdlib, pytest не нужен):
    cd bot && python3 -m unittest discover -s tests -p 'test_config_hot_reload.py' -v
"""
import os
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from config import load_config, reload_config_if_changed  # noqa: E402

BASE = {
    "symbol": "TESTUSDT",
    "timeframe": "5m",
    "leverage": 75,
    "risk_pct": 1.0,
    "sl_pct": 1.0,
    "tp1_pct": 0.6,
    "tp1_close_pct": 50.0,
    "tp2_pct": 1.2,
    "ema_fast": 9,
    "ema_slow": 21,
    "volume_ma_period": 20,
    "volume_multiplier": 1.2,
    "mode": "live",
    "auto_mode": False,
    "backtest_start": "",
    "backtest_end": "",
    "paper_balance": 1000.0,
    "log_file": "test.log",
    "position_size_pct": 0.8,
    "position_size_usd": 0.0,
    "max_position_notional_usd": 0.0,
    "expired_after_minutes": 120,
}


class HotReloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config_test.yaml")
        self.write(BASE)
        self.cfg = load_config(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True)

    def reload(self):
        return reload_config_if_changed(self.cfg, self.path, force=True)

    def test_applies_sizing_change(self):
        self.assertEqual(self.cfg.position_size_pct, 0.8)
        self.write({**BASE, "position_size_pct": 0.3})
        self.assertTrue(self.reload())
        self.assertEqual(self.cfg.position_size_pct, 0.3)

    def test_applies_caps_and_limits(self):
        self.write({
            **BASE,
            "max_position_notional_usd": 50.0,
            "expired_after_minutes": 30,
            "position_loss_pct": 12.5,
        })
        self.assertTrue(self.reload())
        self.assertEqual(self.cfg.max_position_notional_usd, 50.0)
        self.assertEqual(self.cfg.expired_after_minutes, 30)
        self.assertEqual(self.cfg.position_loss_pct, 12.5)

    def test_restart_required_field_not_applied(self):
        self.write({**BASE, "leverage": 50, "timeframe": "15m"})
        self.assertFalse(self.reload())
        self.assertEqual(self.cfg.leverage, 75)
        self.assertEqual(self.cfg.timeframe, "5m")

    def test_no_change_is_noop(self):
        self.assertFalse(self.reload())

    def test_broken_yaml_keeps_old_values(self):
        self.assertEqual(self.cfg.position_size_pct, 0.8)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("symbol: TESTUSDT\nposition_size_pct: [\n")
        self.assertFalse(self.reload())
        self.assertEqual(self.cfg.position_size_pct, 0.8)

    def test_symbol_mismatch_rejected(self):
        self.write({**BASE, "symbol": "OTHERUSDT", "position_size_pct": 0.1})
        self.assertFalse(self.reload())
        self.assertEqual(self.cfg.position_size_pct, 0.8)


if __name__ == "__main__":
    unittest.main()
