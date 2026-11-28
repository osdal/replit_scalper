"""Тесты bot/daily_exchange_pnl.py на моках (без Binance)."""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import daily_exchange_pnl as dep  # noqa: E402


class FakeClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def futures_income_history(self, **kwargs):
        self.calls += 1
        return self.pages[self.calls - 1] if self.calls <= len(self.pages) else []


def _ts(iso):
    from datetime import datetime, timezone
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


def test_pagination_budget_and_kyiv_day(tmp_path, monkeypatch):
    gate = tmp_path / "gate"
    gate.mkdir()
    monkeypatch.setattr(dep, "GATE_GLOB", str(gate / "*.json"))
    monkeypatch.setattr(dep.time, "sleep", lambda _s: None)
    pages = [
        [{"time": _ts("2026-11-24T23:30:00+00:00"), "incomeType": "REALIZED_PNL", "income": "0.5"},
         {"time": _ts("2026-11-25T00:10:00+00:00"), "incomeType": "COMMISSION", "income": "-0.1"},
         {"time": _ts("2026-11-25T00:11:00+00:00"), "incomeType": "TRANSFER", "income": "10"}] * 333,
    ]
    rows, weight, pages_n = dep.fetch_income(FakeClient(pages), 0, _ts("2026-11-26T00:00:00+00:00"), verbose=False)
    assert pages_n == 1
    assert weight == 30
    out = dep.aggregate(rows, 58.5, today="2026-11-25")
    # 25.11 00:10Z = 25.11 02:10 Киев; 23:30Z = 25.11 01:30 Киев
    assert len(out) == 1
    assert out[0]["date"] == "2026-11-25"
    assert abs(out[0]["realized"] - 0.5 * 333) < 1e-6
    assert abs(out[0]["commission"] - -0.1 * 333) < 1e-6
    assert abs(out[0]["transfer"] - 10 * 333) < 1e-6
    assert abs(out[0]["net"] - (0.5 - 0.1) * 333) < 1e-6
    assert out[0]["wallet"] == 58.5


def test_skip_on_gate_and_ban(tmp_path, monkeypatch):
    gate = tmp_path / "gate2"
    gate.mkdir()
    monkeypatch.setattr(dep, "GATE_GLOB", str(gate / "*.json"))
    f = gate / "binance_gate_live.json"
    json.dump({"ts": time.time(), "used_weight_1m": 1000, "ban_until_ms": 0}, open(f, "w"))
    assert dep.gate_blocked() is True
    json.dump({"ts": time.time(), "used_weight_1m": 500,
               "ban_until_ms": int(time.time() * 1000) + 60_000}, open(f, "w"))
    assert dep.gate_blocked() is True
    json.dump({"ts": time.time(), "used_weight_1m": 500, "ban_until_ms": 0}, open(f, "w"))
    assert dep.gate_blocked() is False
    rows, weight, pages = dep.fetch_income(
        FakeClient([[{"time": _ts("2026-11-25T00:00:00+00:00"),
                      "incomeType": "REALIZED_PNL", "income": "1"}]]),
        0, _ts("2026-11-26T00:00:00+00:00"), verbose=False,
    )
    assert len(rows) == 1 and weight == 30 and pages == 1


def test_wallet_only_today_and_budget_override(tmp_path, monkeypatch):
    gate = tmp_path / "gate3"
    gate.mkdir()
    monkeypatch.setattr(dep, "GATE_GLOB", str(gate / "*.json"))
    monkeypatch.setattr(dep.time, "sleep", lambda _s: None)
    rows = [
        {"time": _ts("2026-11-26T10:00:00+00:00"), "incomeType": "REALIZED_PNL", "income": "1"},
        {"time": _ts("2026-11-27T10:00:00+00:00"), "incomeType": "REALIZED_PNL", "income": "2"},
    ]
    out = dep.aggregate(rows, 58.5, today="2026-11-27")
    assert [r["date"] for r in out] == ["2026-11-26", "2026-11-27"]
    assert out[0]["wallet"] == ""
    assert out[1]["wallet"] == 58.5
    page1 = [dict(rows[0], time=_ts("2026-11-26T10:00:00+00:00"), income="1")] * 1000
    page2 = [dict(rows[1], time=_ts("2026-11-27T10:00:00+00:00"), income="2")] * 1000
    client = FakeClient([page1, page2])
    got, weight, pages = dep.fetch_income(
        client, 0, _ts("2026-11-28T00:00:00+00:00"), verbose=False, max_pages=2,
    )
    assert pages == 2 and weight == 60 and len(got) == 2000
