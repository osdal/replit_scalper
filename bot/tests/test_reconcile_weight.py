"""Тесты fix/reconcile-weight: 1 income на весь аккаунт, останов на 429, пропуск при бане."""
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
    def __init__(self, income=None, fills=None, exc=None, raise_on=("income",)):
        self.income = income or []
        self.fills = fills or []
        self.exc = exc
        self.raise_on = raise_on
        self.income_calls = []
        self.fills_calls = []
        self.response = SimpleNamespace(headers={"X-MBX-USED-WEIGHT-1M": "50"})

    def futures_income_history(self, **kw):
        self.income_calls.append(kw)
        if self.exc and "income" in self.raise_on:
            raise self.exc
        return self.income

    def futures_account_trades(self, symbol, startTime, endTime, limit):
        self.fills_calls.append(symbol)
        if self.exc and "fills" in self.raise_on:
            raise self.exc
        return self.fills

    def close_connection(self):
        pass


def _run_main(argv, db, cli, ban=False, weight=20, gate_skip=None):
    with mock.patch.object(sys, "argv", argv), \
         mock.patch.dict(os.environ, {"BINANCE_API_KEY": "k", "BINANCE_API_SECRET": "s"}), \
         mock.patch.object(rc, "_load_env", lambda: None), \
         mock.patch.object(rc, "_gate_skips", gate_skip if gate_skip is not None else (lambda path: ban)), \
         mock.patch.object(rc, "_resp_weight", lambda c: weight), \
         mock.patch.object(rc, "_maybe_pause", lambda *a, **k: None), \
         mock.patch.object(rc, "time", mock.Mock(sleep=lambda s: None)), \
         mock.patch("binance.client.Client", return_value=cli):
        return rc.main()


def test_single_income_call_and_per_symbol_trades_query(tmp_path):
    db = _make_db(str(tmp_path))
    cli = FakeClient()
    argv = ["reconcile_pnl", "--db", db, "--log", str(tmp_path / "log.txt"), "--hours", "48"]
    code = _run_main(argv, db, cli)
    assert code == 0
    assert len(cli.income_calls) == 1   # один futures_income_history на весь аккаунт
    assert len(cli.fills_calls) == 2     # филлы спрашиваются только по символам с строками


def test_stops_on_429(tmp_path):
    db = _make_db(str(tmp_path))
    exc = BinanceAPIException(None, 429, '{"code": -429, "msg": "Too many requests"}')
    cli = FakeClient(exc=exc)
    argv = ["reconcile_pnl", "--db", db, "--log", str(tmp_path / "log.txt"), "--hours", "48"]
    code = _run_main(argv, db, cli)
    assert code != 0
    assert cli.fills_calls == []         # не делали после rate-limit


def test_skips_when_gate_banned(tmp_path):
    db = _make_db(str(tmp_path))
    cli = FakeClient()
    argv = ["reconcile_pnl", "--db", db, "--log", str(tmp_path / "log.txt"), "--hours", "48"]
    code = _run_main(argv, db, cli, gate_skip=lambda path: True)
    assert code == 0
    assert cli.income_calls == []        # ни одного запроса при бане
    assert cli.fills_calls == []
