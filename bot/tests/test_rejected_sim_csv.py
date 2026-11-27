"""Тест записи исходов симулированных отклонённых сигналов в CSV (live only)."""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main as m  # noqa: E402

HEADER = ["symbol", "direction", "entry_time", "exit_time",
          "simulated_pnl", "reason", "reject_reason"]


def _sim(symbol="XMRUSDT"):
    return {
        "symbol": symbol,
        "direction": "SHORT",
        "entry_time": "2026-11-27T19:00:00+00:00",
        "reject_reason": "max_positions",
    }


def test_append_live_csv(tmp_path, monkeypatch):
    path = tmp_path / "rejected_sim_live.csv"
    monkeypatch.setattr(m, "_REJECTED_SIM_CSV", str(path))
    monkeypatch.setattr(m, "BOT_ENV", "live")

    m._append_rejected_sim_csv(_sim(), "TP1", "2026-11-27T19:05:00+00:00", 0.1234567)
    m._append_rejected_sim_csv(_sim("VVVUSDT"), "SL", "2026-11-27T19:10:00+00:00", -0.05)

    with open(path, newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0].keys()) == HEADER
    assert len(rows) == 2
    assert rows[0] == {
        "symbol": "XMRUSDT", "direction": "SHORT",
        "entry_time": "2026-11-27T19:00:00+00:00",
        "exit_time": "2026-11-27T19:05:00+00:00",
        "simulated_pnl": "0.123457", "reason": "TP1",
        "reject_reason": "max_positions",
    }
    assert rows[1]["reason"] == "SL"
    assert rows[1]["simulated_pnl"] == "-0.05"


def test_non_live_does_not_write(tmp_path, monkeypatch):
    path = tmp_path / "rejected_sim_live.csv"
    monkeypatch.setattr(m, "_REJECTED_SIM_CSV", str(path))
    monkeypatch.setattr(m, "BOT_ENV", "testnet")
    m._append_rejected_sim_csv(_sim(), "TP1", "2026-11-27T19:05:00+00:00", 1.0)
    assert not path.exists()
