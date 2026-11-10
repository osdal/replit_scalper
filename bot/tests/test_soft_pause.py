"""Мягкая пауза при занятых слотах (SLOTS_FULL_SOFT_PAUSE).

Пока все слоты позиций заняты, остальные боты должны только буферизовать
свечи и не считать сигналы/не писать rejected-строки. Пауза срабатывает
ИСКЛЮЧИТЕЛЬНО на reason=max_positions: loss_streak/daily_limit/check_error
не должны «замораживать» бота.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import main  # noqa: E402


def test_pause_on_max_positions():
    state = {"allowed": False, "reason": "max_positions",
             "positions_open": 3, "max_positions": 3}
    assert main._should_soft_pause(state) is True


def test_no_pause_when_allowed():
    assert main._should_soft_pause({"allowed": True, "reason": None}) is False


def test_no_pause_on_other_reasons():
    for reason in ("loss_streak", "pause", "daily_loss_limit", "check_error"):
        assert main._should_soft_pause(
            {"allowed": False, "reason": reason}
        ) is False, reason


def test_no_pause_on_bad_state():
    assert main._should_soft_pause(None) is False
    assert main._should_soft_pause({}) is False
    assert main._should_soft_pause("error") is False
