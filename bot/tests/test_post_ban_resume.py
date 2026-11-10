"""Post-ban resume stagger в AccountGate.

Проблема (testnet, 10.11.2026): бан истёк в 09:24:55, а в 09:25:30-09:25:51
все 8 ботов уже получили новый бан — флот выходит в сеть залпом на первой же
свече. Решение: после `ban_until` общий cooldown + индивидуальная задержка
процесса, и только потом запросы.
"""
import asyncio
import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from binance_gate import AccountGate, GateBlocked  # noqa: E402
import rate_limit as rl  # noqa: E402


def _write(path, ban_until_ms):
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"ban_until_ms": ban_until_ms, "used_weight_1m": 10,
                   "ts": time.time(), "local_weight_1m": 5}, f)


def _gate(tmp_path, cooldown, jitter):
    os.environ["POST_BAN_COOLDOWN_SEC"] = str(cooldown)
    os.environ["POST_BAN_JITTER_SEC"] = str(jitter)
    try:
        return AccountGate(path=tmp_path / "gate.json", weight_limit_1m=2400, shed_at=0.75)
    finally:
        os.environ.pop("POST_BAN_COOLDOWN_SEC", None)
        os.environ.pop("POST_BAN_JITTER_SEC", None)


def test_expired_ban_blocks_during_cooldown(tmp_path):
    p = tmp_path / "gate.json"
    ban_until = int((time.time() - 10) * 1000)  # бан снят 10с назад
    _write(p, ban_until)
    gate = _gate(tmp_path, cooldown=60, jitter=0)

    allowed, reason = gate.check_and_reserve(weight=5, is_critical=True)
    assert allowed is False
    assert reason.startswith("post-ban resume")
    # ~50 секунд осталось
    left = float(reason.split()[-1].rstrip("s"))
    assert 35 <= left <= 55


def test_old_expired_ban_does_not_block(tmp_path):
    p = tmp_path / "gate.json"
    _write(p, int((time.time() - 600) * 1000))  # бан снят 10 минут назад
    gate = _gate(tmp_path, cooldown=60, jitter=0)

    allowed, reason = gate.check_and_reserve(weight=5, is_critical=False)
    assert allowed is True, reason


def test_disabled_by_default(tmp_path):
    p = tmp_path / "gate.json"
    _write(p, int((time.time() - 5) * 1000))
    gate = _gate(tmp_path, cooldown=0, jitter=0)

    allowed, reason = gate.check_and_reserve(weight=5, is_critical=True)
    assert allowed is True, reason
    assert gate._resume_jitter == 0.0


def test_jitter_is_bounded_and_stable(tmp_path):
    gate = _gate(tmp_path, cooldown=0, jitter=300)
    assert 0.0 <= gate._resume_jitter < 300.0
    assert gate._resume_jitter == gate._calc_resume_jitter(300)


def test_post_ban_block_is_ban_like_for_rate_limit(tmp_path):
    """Gate отдаёт код -1003, а wait_for_ban не спит и не считает это баном."""
    p = tmp_path / "gate.json"
    _write(p, int((time.time() - 1) * 1000))
    gate = _gate(tmp_path, cooldown=120, jitter=0)

    allowed, reason = gate.check_and_reserve(weight=5, is_critical=False)
    assert allowed is False
    exc = GateBlocked(reason, code=-1003)

    rl.reset_ban_state()
    t0 = time.monotonic()
    handled = asyncio.run(rl.wait_for_ban(exc))
    elapsed = time.monotonic() - t0

    assert handled is True
    assert elapsed < 0.5, f"wait_for_ban уснул на {elapsed:.2f}s в hot path"
    # Это не бан: глобальное окно бана не должно появиться.
    assert rl._active_ban_until_wall == 0.0
    rl.reset_ban_state()
