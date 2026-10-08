"""Тесты клиентской части резервирования слота (bot/recovery_client.py).

Запуск:  cd bot && python -m pytest tests/test_slot_reservation_client.py -q
"""
import asyncio
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
pytest.importorskip("aiohttp")
import recovery_client  # noqa: E402
from recovery_client import RecoveryClient  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class FakeResp:
    def __init__(self, status, data):
        self.status = status
        self._data = data

    async def json(self):
        return self._data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class FakeSession:
    def __init__(self, responder):
        self.responder = responder
        self.posts = []

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        out = self.responder(url, json)
        if isinstance(out, Exception):
            raise out
        return out


def make_client(responder):
    c = RecoveryClient("BTCUSDT", logging.getLogger("slot-test"))
    sess = FakeSession(responder)

    async def _get_session():
        return sess

    c._get_session = _get_session
    return c, sess


def test_peek_does_not_reserve_and_sends_flag_false():
    c, sess = make_client(lambda u, j: FakeResp(200, {"allowed": True, "positions_open": 1}))
    data = run(c.can_open())
    assert data["allowed"] is True
    assert sess.posts[0][1] == {"symbol": "BTCUSDT", "reserve": False}
    assert c._slot_id is None
    run(c.release_slot())          # без резерва — no-op
    assert len(sess.posts) == 1


def test_reserve_stores_token_and_release_posts_once():
    def responder(url, body):
        if url.endswith("/trading/check"):
            return FakeResp(200, {"allowed": True, "reservation_id": "abc-1", "own_slot": False})
        return FakeResp(200, {"released": True})

    c, sess = make_client(responder)
    data = run(c.can_open(reserve=True))
    assert data["reservation_id"] == "abc-1"
    assert sess.posts[0][1] == {"symbol": "BTCUSDT", "reserve": True}
    assert c._slot_id == "abc-1"

    run(c.release_slot())
    assert sess.posts[1][0].endswith("/trading/release")
    assert sess.posts[1][1] == {"symbol": "BTCUSDT", "reservation_id": "abc-1"}
    assert c._slot_id is None
    run(c.release_slot())          # повторный release — no-op
    assert len(sess.posts) == 2


def test_denied_reserve_keeps_no_token():
    c, sess = make_client(lambda u, j: FakeResp(200, {"allowed": False, "reason": "max_positions"}))
    data = run(c.can_open(reserve=True))
    assert data["allowed"] is False
    assert c._slot_id is None
    run(c.release_slot())
    assert len(sess.posts) == 1


def test_own_slot_without_reservation_id_has_nothing_to_release():
    c, sess = make_client(lambda u, j: FakeResp(200, {"allowed": True, "own_slot": True, "reservation_id": None}))
    run(c.can_open(reserve=True))
    assert c._slot_id is None
    run(c.release_slot())
    assert len(sess.posts) == 1


def test_fail_closed_on_server_error_and_exception():
    c, _ = make_client(lambda u, j: FakeResp(500, {}))
    assert run(c.can_open(reserve=True)) == {"allowed": False, "reason": "check_error"}
    c2, _ = make_client(lambda u, j: RuntimeError("net down"))
    assert run(c2.can_open(reserve=True)) == {"allowed": False, "reason": "check_error"}
    assert c2._slot_id is None


def test_release_errors_are_swallowed_and_token_cleared():
    def responder(url, body):
        if url.endswith("/trading/check"):
            return FakeResp(200, {"allowed": True, "reservation_id": "x"})
        return RuntimeError("release failed")

    c, _ = make_client(responder)
    run(c.can_open(reserve=True))
    run(c.release_slot())          # не бросает
    assert c._slot_id is None
