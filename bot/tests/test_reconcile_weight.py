"""Тесты fix/reconcile-weight: 1 income на весь аккаунт, пагинация, локальный
бюджет weight, пауза перед trades, останов на 429, пропуск при бане.
"""
import os
import sys
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import reconcile_pnl as rc  # noqa: E402
from binance.exceptions import BinanceAPIException  # noqa: E402


def _make_db(tmp: str, symbols=("AAAUSDT", "BBBUSDT")) -> str:
    p = os.path.join(tmp, "bot.db")
    c = sqlite3.connect(p)
    c.execute(
        """CREATE TABLE trades (
            id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT,
            pnl REAL, commission REAL, exit_reason TEXT, cycle_pnl REAL, leg_pnl REAL,
            entry_price REAL, entry_time TEXT, exit_time TEXT, chain_depth INTEGER,
            is_open INTEGER DEFAULT 0, status TEXT, mode TEXT)"""
    )
    now = datetime.now(timezone.utc)
    for i, s in enumerate(symbols):
        c.execute(
            "INSERT INTO trades (id, symbol, direction, pnl, commission, exit_reason, mode, status, "
            "entry_time, exit_time, is_open) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
            (i + 1, s, "LONG", 0.5, 0.01, "TP1", "live", "closed",
             (now - timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%S"),
             (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S")),
        )
    c.commit()
    c.close()
    return p


class FakeClient:
    def __init__(self, income=None, fills=None, exc=None, raise_on=("income",),
                 income_pages=None, events=None):
        self.income = income or []
        self.fills = fills or []
        self.income_pages = income_pages
        self.exc = exc
        self.raise_on = raise_on
        self.income_calls = []
        self.fills_calls = []
        self.events = events if events is not None else []
        self.response = SimpleNamespace(headers={"X-MBX-USED-WEIGHT-1M": "50"})

    def futures_income_history(self, **kw):
        self.income_calls.append(kw)
        self.events.append(("income", kw.get("startTime")))
        if self.exc and "income" in self.raise_on:
            raise self.exc
        if self.income_pages is not None:
            i = len(self.income_calls) - 1
            return self.income_pages[i] if i < len(self.income_pages) else []
        return self.income

    def futures_account_trades(self, symbol, startTime, endTime, limit):
        self.fills_calls.append(symbol)
        self.events.append(("trades", symbol))
        if self.exc and "fills" in self.raise_on:
            raise self.exc
        return self.fills

    def close_connection(self):
        pass


def _run_main(argv, cli, ban=False, weight=20, sleep=None):
    sleep_mock = mock.Mock(side_effect=sleep)
    with mock.patch.object(sys, "argv", argv), \
         mock.patch.dict(os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}), \
         mock.patch.object(rc, "_load_env", lambda: None), \
         mock.patch.object(rc, "_gate_skips", lambda path: ban), \
         mock.patch.object(rc, "_resp_weight", lambda c: weight), \
         mock.patch.object(rc, "time", mock.Mock(sleep=sleep_mock)), \
         mock.patch("binance.client.Client", return_value=cli):
        return rc.main()


def test_single_income_call_and_per_symbol_trades_query(tmp_path):
    db = _make_db(str(tmp_path))
    cli = FakeClient()
    argv = ["reconcile_pnl", "--db", db, "--log", str(tmp_path / "log.txt"), "--hours", "48"]
    assert _run_main(argv, cli) == 0
    assert len(cli.income_calls) == 1   # один income на весь аккаунт
    assert len(cli.fills_calls) == 2    # trades только по символам с строками


def test_stops_on_429(tmp_path):
    db = _make_db(str(tmp_path))
    exc = BinanceAPIException(None, 429, '{"code": -429, "msg": "Too many requests"}')
    cli = FakeClient(exc=exc)
    argv = ["reconcile_pnl", "--db", db, "--log", str(tmp_path / "log.txt"), "--hours", "48"]
    assert _run_main(argv, cli) != 0
    assert cli.fills_calls == []


def test_skips_when_gate_banned(tmp_path):
    db = _make_db(str(tmp_path))
    cli = FakeClient()
    argv = ["reconcile_pnl", "--db", db, "--log", str(tmp_path / "log.txt"), "--hours", "48"]
    assert _run_main(argv, cli, ban=True) == 0
    assert cli.income_calls == []
    assert cli.fills_calls == []


def test_income_pagination_uses_last_time_plus_one(tmp_path):
    """1000 строк -> следующая страница начинается с time последней + 1 мс."""
    db = _make_db(str(tmp_path), symbols=("AAAUSDT",))
    base = int(datetime.now(timezone.utc).timestamp() * 1000) - 3 * 3_600_000
    page1 = [
        {"symbol": "AAAUSDT", "incomeType": "FUNDING_FEE", "time": base + i, "income": "0.01"}
        for i in range(1000)
    ]
    page2 = [
        {"symbol": "AAAUSDT", "incomeType": "FUNDING_FEE", "time": base + 1000 + i, "income": "0.01"}
        for i in range(3)
    ]
    cli = FakeClient(income_pages=[page1, page2])
    argv = ["reconcile_pnl", "--db", db, "--log", str(tmp_path / "log.txt"), "--hours", "48"]
    assert _run_main(argv, cli) == 0
    assert len(cli.income_calls) == 2
    assert cli.income_calls[1]["startTime"] == base + 1000
    assert cli.income_calls[0]["startTime"] != cli.income_calls[1]["startTime"]


def test_budget_counts_own_weight_not_header(tmp_path):
    """Заголовок показывает 5000 (весь IP), но бюджет 40 тратится по своим весам:
    1 income (30) + 2 trades (5+5) = 40, третий trades не делается."""
    db = _make_db(str(tmp_path), symbols=("AAAUSDT", "BBBUSDT", "CCCUSDT"))
    cli = FakeClient()
    argv = ["reconcile_pnl", "--db", db, "--log", str(tmp_path / "log.txt"), "--hours", "48"]
    with mock.patch.object(rc, "BUDGET_WEIGHT", 40):
        assert _run_main(argv, cli, weight=5000) == 0
    assert len(cli.income_calls) == 1
    assert len(cli.fills_calls) == 2
    assert "бюджет исчерпан" in (tmp_path / "log.txt").read_text()


def test_pause_before_each_trades_call(tmp_path):
    db = _make_db(str(tmp_path))
    events = []
    cli = FakeClient(events=events)
    argv = ["reconcile_pnl", "--db", db, "--log", str(tmp_path / "log.txt"), "--hours", "48"]
    assert _run_main(argv, cli, sleep=lambda s: events.append(("sleep", s))) == 0
    for i, ev in enumerate(events):
        if ev[0] == "trades":
            assert events[i - 1] == ("sleep", rc.REQ_PAUSE_SEC), events
