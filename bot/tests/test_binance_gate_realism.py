"""Приёмочные тесты binance_gate на РЕАЛЬНОМ поведении python-binance и 40 процессов.

Почему отдельный файл: прежние тесты использовали заглушки, не похожие на настоящий
AsyncClient (ответ с .headers), поэтому не ловили дефекты ниже.

Запуск:  cd bot && python -m pytest tests/test_binance_gate_realism.py -q
"""
import asyncio
import multiprocessing as mp
import os
import sys
import tempfile
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import binance_gate as bg  # noqa: E402


def run(c):
    return asyncio.run(c)


class RealLikeClient:
    """Как python-binance 1.0.37: AsyncClient._request возвращает РАСПАРСЕННЫЙ JSON
    (dict/list), а объект ответа лежит в self.response (там заголовки)."""

    class _Resp:
        headers = {"X-MBX-USED-WEIGHT-1M": "123"}

    def __init__(self):
        self.response = self._Resp()
        self.calls = 0

    async def _request(self, method, uri, signed, force_params=False, **kw):
        self.calls += 1
        self.response = self._Resp()
        return [{"symbol": "BTCUSDT", "positionAmt": "0"}] if "positionRisk" in uri else {"orderId": 1}


def _gate(tmp_path, **kw):
    return bg.AccountGate(path=str(tmp_path / "gate.json"), **kw)


def test_wrapper_never_breaks_a_successful_call(tmp_path):
    """Успешный ответ (в т.ч. НОВЫЙ ОРДЕР) должен доходить до вызывающего кода как есть."""
    c = bg.wrap_client(RealLikeClient(), _gate(tmp_path))
    assert run(c._request("post", "https://fapi.binance.com/fapi/v1/order", True)) == {"orderId": 1}
    assert run(c._request("get", "https://fapi.binance.com/fapi/v2/positionRisk", True))[0]["symbol"] == "BTCUSDT"


def test_gate_bookkeeping_failure_must_not_break_a_successful_call(tmp_path, monkeypatch):
    """Сбой записи в общий файл НЕ должен превращать выполненный запрос в исключение."""
    gate = _gate(tmp_path)
    c = bg.wrap_client(RealLikeClient(), gate)

    def boom(*a, **k):
        raise OSError("disk error")

    monkeypatch.setattr(gate, "_write", boom)
    assert run(c._request("post", "https://fapi.binance.com/fapi/v1/order", True)) == {"orderId": 1}


def test_used_weight_tracks_header_total_not_sum(tmp_path):
    """X-MBX-USED-WEIGHT-1M — это ИТОГ по IP за минуту. Хранить его нужно как есть,
    а не суммировать: иначе счётчик растёт без предела и режет все вторичные запросы."""
    g = _gate(tmp_path)
    for header_total in (100, 150, 200, 250, 300):
        g.record_response(header_total)
    assert g.get_status()["used_weight_1m"] <= 300


def test_steady_traffic_does_not_inflate_counter(tmp_path, monkeypatch):
    g = _gate(tmp_path)
    t = [time.time()]
    monkeypatch.setattr(bg, "_now_sec", lambda: t[0])
    for i in range(300):          # 5 минут ровного трафика, реальный итог ~120/мин
        t[0] += 1.0
        g.record_response(120)
    assert g.get_status()["used_weight_1m"] <= 240
    assert g.check_and_reserve(1, is_critical=False)[0] is True


@pytest.mark.parametrize("method,uri", [
    ("get", "https://fapi.binance.com/fapi/v1/exchangeInfo"),
    ("get", "https://fapi.binance.com/fapi/v1/leverageBracket"),
    ("post", "https://fapi.binance.com/fapi/v1/leverage"),
    ("get", "https://fapi.binance.com/fapi/v1/openOrders"),
])
def test_startup_and_protection_calls_are_never_shed(tmp_path, method, uri):
    """Старт бота и снятие/проверка защитных ордеров нельзя резать по весу, только при бане."""
    g = _gate(tmp_path)
    g.record_response(2300)       # почти предел
    assert g.check_and_reserve(bg._estimate_weight(method, uri), is_critical=bg._is_critical(method, uri))[0] is True


def _hammer(path, n, q):
    gate = bg.AccountGate(path=path)
    errs = 0
    for _ in range(n):
        try:
            gate.record_response(5)
            gate.check_and_reserve(1, False)
        except Exception:
            errs += 1
    q.put(errs)


def _ban(path, ban_ms, q):
    time.sleep(0.15)
    bg.AccountGate(path=path).record_ban(ban_ms)
    q.put(0)


def test_shared_state_is_safe_under_many_processes_and_keeps_the_ban():
    """8 процессов пишут вес, один объявляет бан: ни одного исключения, бан не теряется."""
    path = os.path.join(tempfile.mkdtemp(), "gate.json")
    ban_ms = int(time.time() * 1000) + 600_000
    q = mp.Queue()
    ps = [mp.Process(target=_hammer, args=(path, 300, q)) for _ in range(8)]
    ps.append(mp.Process(target=_ban, args=(path, ban_ms, q)))
    for p in ps:
        p.start()
    for p in ps:
        p.join()
    errs = sum(q.get() for _ in ps)
    assert errs == 0
    assert bg.AccountGate(path=path).is_banned(), "бан, записанный одним процессом, потерян"
