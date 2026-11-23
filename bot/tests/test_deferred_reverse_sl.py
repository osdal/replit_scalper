"""Отложенный SL первой ноги при бане Binance (main._defer_reverse_sl_for_rate_limit).

Сценарий из прода: часовой IP-бан (-1003) блокирует открытие reverse-ноги;
ретраи 3×2с проваливаются, и первая нога закрывалась по SL (ETHFI/KAS/PUMP,
суммарно −$0.264). Теперь при бановом отказе SL откладывается до снятия бана,
а прочие отказы (dust, занятые слоты, отказ по существу) закрываются как раньше.

Запуск:  cd bot && python -m pytest tests/test_deferred_reverse_sl.py -q
"""
import logging
import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402

LOG = logging.getLogger("test-deferred-reverse-sl")
SYM = "ETHFIUSDT"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    main._deferred_reverse_sl.clear()
    monkeypatch.setattr(main, "_DEFERRED_REVERSE_SL_MAX_SEC", 180.0)
    monkeypatch.setattr(main, "_DEFERRED_REVERSE_SL_RETRY_SEC", 5.0)
    yield
    main._deferred_reverse_sl.clear()


def om(reason):
    return SimpleNamespace(last_reverse_skip_reason=reason)


@pytest.mark.parametrize("reason", [None, "dust", "max_positions", "exchange_reject"])
def test_no_deferral_for_non_rate_limit_reason(reason):
    assert main._defer_reverse_sl_for_rate_limit(SYM, om(reason), LOG, 0.53, now=1000.0) is False
    assert not main._is_reverse_sl_deferred(SYM)


def test_defers_on_rate_limit_and_schedules_retry():
    assert main._defer_reverse_sl_for_rate_limit(SYM, om("rate_limit"), LOG, 0.53, now=1000.0) is True
    assert main._is_reverse_sl_deferred(SYM)
    # раньше 5с ретрай не созрел
    assert main._due_deferred_reverse_sl(SYM, now=1004.0) is None
    assert main._due_deferred_reverse_sl(SYM, now=1005.0) == pytest.approx(0.53)


def test_budget_exhausted_closes_and_clears():
    assert main._defer_reverse_sl_for_rate_limit(SYM, om("rate_limit"), LOG, 0.53, now=1000.0) is True
    # бюджет 180с исчерпан — ногу закрываем как раньше
    assert main._defer_reverse_sl_for_rate_limit(SYM, om("rate_limit"), LOG, 0.53, now=1181.0) is False
    assert not main._is_reverse_sl_deferred(SYM)


def test_clear_on_success_or_flat():
    main._defer_reverse_sl_for_rate_limit(SYM, om("rate_limit"), LOG, 0.53, now=1000.0)
    main._clear_deferred_reverse_sl(SYM)
    assert not main._is_reverse_sl_deferred(SYM)
    assert main._due_deferred_reverse_sl(SYM, now=2000.0) is None


def test_attempts_accumulate_within_budget():
    for t in (1000.0, 1010.0, 1020.0):
        assert main._defer_reverse_sl_for_rate_limit(SYM, om("rate_limit"), LOG, 0.53, now=t) is True
    assert main._deferred_reverse_sl[SYM]["attempts"] == 3
    # бюджет считается от ПЕРВОЙ отсрочки, а не от последней
    assert main._defer_reverse_sl_for_rate_limit(SYM, om("rate_limit"), LOG, 0.53, now=1181.0) is False


def test_symbols_are_independent():
    main._defer_reverse_sl_for_rate_limit(SYM, om("rate_limit"), LOG, 0.53, now=1000.0)
    assert main._due_deferred_reverse_sl("KASUSDT", now=2000.0) is None
    assert main._is_reverse_sl_deferred(SYM)
