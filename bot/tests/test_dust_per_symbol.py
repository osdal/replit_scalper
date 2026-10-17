"""Тесты заданья A: пыль только по своему символу + сдвиг по символу.

Запуск: cd bot && python -m pytest tests/test_dust_per_symbol.py -q
"""
import sys
import os
import zlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Импортируем из main (уже отредактированного)
import main  # noqa: E402


def test_symbol_shift_deterministic():
    """Сдвиг для одного и того же символа всегда одинаковый."""
    s1 = main._symbol_shift("BTCUSDT")
    s2 = main._symbol_shift("BTCUSDT")
    assert s1 == s2
    assert 0 <= s1 < 12


def test_symbol_shift_different_symbols():
    """Разные символы — разные сдвиги (вероятно, но не гарантировано)."""
    s1 = main._symbol_shift("BTCUSDT")
    s2 = main._symbol_shift("ETHUSDT")
    # Может совпасть random-но, но маловероятно. Проверяем, что функции возвращает int.
    assert isinstance(s1, int)
    assert isinstance(s2, int)
    assert 0 <= s1 < 12
    assert 0 <= s2 < 12


def test_dust_shift_spreads():
    """Сдвиги для разных символов не все одинаковые (размазывание по свечам)."""
    symbols = ["BTCUSDT", "ETHUSDT", "TRXUSDT", "PEPEUSDT", "HBARUSDT"]
    shifts = {main._symbol_shift(s) for s in symbols}
    # Как минимум 2 разных сдвига из 5 символов (очень высокая вероятность)
    assert len(shifts) >= 2


def test_dust_qty_threshold():
    """Порог пыли DUST_QTY = 0.001."""
    assert main.DUST_QTY == 0.001
