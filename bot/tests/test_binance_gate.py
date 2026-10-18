"""Тесты AccountGate (bot/binance_gate.py).

Запуск: cd bot && python -m pytest tests/test_binance_gate.py -q
"""
import asyncio
import json
import os
import sys
import tempfile
from types import SimpleNamespace

import pytest

from binance.exceptions import BinanceAPIException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from binance_gate import AccountGate, wrap_client, _estimate_weight, _is_critical  # noqa: E402


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- #
#  Helpers                                                                    #
# --------------------------------------------------------------------------- #
def write_gate(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def read_gate(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- #
#  AccountGate: check_and_reserve                                             #
# --------------------------------------------------------------------------- #
def test_check_and_reserve_ok(tmp_path):
    p = tmp_path / "gate.json"
    write_gate(p, {"ban_until_ms": 0, "used_weight_1m": 100, "ts": __import__("time").time()})
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)
    allowed, reason = gate.check_and_reserve(weight=1, is_critical=False)
    assert allowed is True
    assert reason == "ok"


def test_check_and_reserve_shed(tmp_path):
    p = tmp_path / "gate.json"
    # used_weight = 1900 > 75% of 2400 = 1800 → secondary skipped
    write_gate(p, {"ban_until_ms": 0, "used_weight_1m": 1900, "ts": __import__("time").time()})
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)
    allowed, reason = gate.check_and_reserve(weight=1, is_critical=False)
    assert allowed is False
    assert "shed" in reason


def test_check_and_reserve_critical_passes_shed(tmp_path):
    p = tmp_path / "gate.json"
    write_gate(p, {"ban_until_ms": 0, "used_weight_1m": 2200, "ts": __import__("time").time()})
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)
    allowed, reason = gate.check_and_reserve(weight=1, is_critical=True)
    assert allowed is True
    assert reason == "ok"


def test_check_and_reserve_banned(tmp_path):
    p = tmp_path / "gate.json"
    import time
    write_gate(p, {"ban_until_ms": int(time.time() * 1000) + 60000, "used_weight_1m": 0, "ts": time.time()})
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)
    allowed, reason = gate.check_and_reserve(weight=1, is_critical=False)
    assert allowed is False
    assert "banned" in reason


def test_check_and_reserve_banned_critical_still_blocked(tmp_path):
    p = tmp_path / "gate.json"
    import time
    write_gate(p, {"ban_until_ms": int(time.time() * 1000) + 60000, "used_weight_1m": 0, "ts": time.time()})
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)
    allowed, reason = gate.check_and_reserve(weight=1, is_critical=True)
    assert allowed is False
    assert "banned" in reason


# --------------------------------------------------------------------------- #
#  AccountGate: record_response + ban                                         #
# --------------------------------------------------------------------------- #
def test_record_response_replaces_not_sums(tmp_path):
    """X-MBX-USED-WEIGHT-1M — итог за минуту, сохраняется как есть (замена, не сложение)."""
    p = tmp_path / "gate.json"
    write_gate(p, {"ban_until_ms": 0, "used_weight_1m": 100, "ts": __import__("time").time()})
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)
    gate.record_response(weight_used=50)
    data = read_gate(gate._my_path())
    assert data["used_weight_1m"] == 50  # замена, не сложение


def test_record_response_resets_window(tmp_path):
    p = tmp_path / "gate.json"
    import time
    # Stale window (ts > 60s ago)
    write_gate(p, {"ban_until_ms": 0, "used_weight_1m": 9999, "ts": time.time() - 120})
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)
    gate.record_response(weight_used=10)
    data = read_gate(gate._my_path())
    assert data["used_weight_1m"] == 10


def test_record_ban(tmp_path):
    p = tmp_path / "gate.json"
    write_gate(p, {"ban_until_ms": 0, "used_weight_1m": 0, "ts": __import__("time").time()})
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)
    gate.record_ban(ban_until_ms=int(__import__("time").time() * 1000) + 60000)
    assert gate.is_banned() is True


# --------------------------------------------------------------------------- #
#  Fail-open                                                                  #
# --------------------------------------------------------------------------- #
def test_fail_open_corrupt_file(tmp_path):
    p = tmp_path / "gate.json"
    with open(p, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)
    allowed, reason = gate.check_and_reserve(weight=1, is_critical=False)
    assert allowed is True  # fail-open


def test_fail_open_missing_file(tmp_path):
    p = tmp_path / "nonexistent.json"
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)
    allowed, reason = gate.check_and_reserve(weight=1, is_critical=False)
    assert allowed is True


# --------------------------------------------------------------------------- #
#  _estimate_weight / _is_critical                                            #
# --------------------------------------------------------------------------- #
def test_estimate_weight_account():
    assert _estimate_weight("GET", "/fapi/v1/account") == 5
    assert _estimate_weight("GET", "/fapi/v1/positionRisk") == 5
    assert _estimate_weight("GET", "/fapi/v1/ticker/price") == 1
    assert _estimate_weight("GET", "/fapi/v1/klines") == 1
    assert _estimate_weight("POST", "/fapi/v1/order") == 1
    assert _estimate_weight("GET", "/fapi/v1/unknown") == 1


def test_is_critical():
    assert _is_critical("POST", "/fapi/v1/order") is True
    assert _is_critical("DELETE", "/fapi/v1/order") is True
    assert _is_critical("GET", "/fapi/v1/account") is True
    assert _is_critical("GET", "/fapi/v1/positionRisk") is True
    assert _is_critical("GET", "/fapi/v1/exchangeInfo") is True
    assert _is_critical("GET", "/fapi/v1/leverageBracket") is True
    assert _is_critical("POST", "/fapi/v1/leverage") is True
    assert _is_critical("GET", "/fapi/v1/openOrders") is True
    assert _is_critical("GET", "/fapi/v1/klines?limit=100") is True
    assert _is_critical("GET", "/fapi/v1/klines?limit=99") is False
    assert _is_critical("GET", "/fapi/v1/ticker/price") is False
    assert _is_critical("GET", "/fapi/v1/klines") is False


# --------------------------------------------------------------------------- #
#  wrap_client                                                                #
# --------------------------------------------------------------------------- #
class FakeResponse:
    def __init__(self, headers=None, text=""):
        self.headers = headers or {}
        self._text = text

    async def text(self):
        return self._text


class FakeClient:
    def __init__(self, should_raise=None):
        self.should_raise = should_raise
        self.calls = []
        self.response = None

    async def _request(self, method, uri, signed, force_params=False, **kwargs):
        self.calls.append((method, uri))
        if self.should_raise:
            raise self.should_raise
        resp = FakeResponse(headers={"X-MBX-USED-WEIGHT-1M": "3"})
        self.response = resp
        return resp


def test_wrap_client_allows_and_records(tmp_path):
    p = tmp_path / "gate.json"
    write_gate(p, {"ban_until_ms": 0, "used_weight_1m": 0, "ts": __import__("time").time()})
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)
    client = FakeClient()
    wrap_client(client, gate)

    resp = run(client._request("GET", "/fapi/v1/ticker/price", signed=False, symbol="BTCUSDT"))
    assert resp.headers.get("X-MBX-USED-WEIGHT-1M") == "3"
    data = read_gate(gate._my_path())
    assert data["used_weight_1m"] == 3  # фактический вес из заголовка ответа


def test_wrap_client_sheds_secondary(tmp_path):
    p = tmp_path / "gate.json"
    write_gate(p, {"ban_until_ms": 0, "used_weight_1m": 2200, "ts": __import__("time").time()})
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)
    client = FakeClient()
    wrap_client(client, gate)

    with pytest.raises(Exception) as exc:
        run(client._request("GET", "/fapi/v1/ticker/price", signed=False, symbol="BTCUSDT"))
    assert exc.value.code == -429

def test_wrap_client_records_ban(tmp_path):
    p = tmp_path / "gate.json"
    write_gate(p, {"ban_until_ms": 0, "used_weight_1m": 0, "ts": __import__("time").time()})
    gate = AccountGate(path=p, weight_limit_1m=2400, shed_at=0.75)

    class FakeResponse:
        text = "IP banned until 99999999999"

    class BanClient:
        async def _request(self, method, uri, signed, force_params=False, **kwargs):
            e = Exception("IP banned until 99999999999")
            e.code = -1003
            raise e

    client = BanClient()
    wrap_client(client, gate)
    with pytest.raises(Exception) as exc:
        run(client._request("GET", "/fapi/v1/ticker/price", signed=False, symbol="BTCUSDT"))
    assert exc.value.code == -1003
    data = read_gate(gate._my_path())
    assert data["ban_until_ms"] == 99999999999
