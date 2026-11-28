"""Тесты bot/quant_status_poll.py на моках (без Binance)."""
import csv
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import quant_status_poll as qsp  # noqa: E402


def test_gate_skip(tmp_path, monkeypatch):
    d = tmp_path / "gate"
    d.mkdir()
    monkeypatch.setattr(qsp, "GATE_GLOB", str(d / "*.json"))
    f = d / "binance_gate_live.json"
    json.dump({"ts": time.time(), "used_weight_1m": 1000, "ban_until_ms": 0}, open(f, "w"))
    assert qsp.gate_blocked() is True
    json.dump({"ts": time.time(), "used_weight_1m": 500,
               "ban_until_ms": int(time.time() * 1000) + 60000}, open(f, "w"))
    assert qsp.gate_blocked() is True
    json.dump({"ts": time.time(), "used_weight_1m": 500, "ban_until_ms": 0}, open(f, "w"))
    assert qsp.gate_blocked() is False


def test_empty_response_one_none_row(tmp_path):
    now = datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc)
    rows = qsp.build_rows({}, now)
    assert len(rows) == 1
    assert rows[0]["symbol"] == "none"
    assert rows[0]["indicator"] == "none"


def test_symbol_rows_and_csv_append(tmp_path):
    data = {"symbols": [{
        "symbol": "XMRUSDT", "isLocked": True, "plannedRecoverTime": 1795873000000,
        "indicators": [{"indicator": "TMV", "value": 12.5, "triggerValue": 10}],
    }]}
    rows = qsp.build_rows(data, datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc))
    assert len(rows) == 1
    r = rows[0]
    assert r["symbol"] == "XMRUSDT" and r["indicator"] == "TMV"
    assert r["isLocked"] is True and r["plannedRecoverTime"] == 1795873000000
    path = str(tmp_path / "quant_status_live.csv")
    qsp.append_rows(rows, path)
    qsp.append_rows(rows, path)
    with open(path, newline="", encoding="utf-8") as fh:
        got = list(csv.DictReader(fh))
    assert len(got) == 2
    assert got[0]["indicator"] == "TMV"
