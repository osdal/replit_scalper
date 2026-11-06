"""Общие фикстуры тестов бота.

Важно: некоторые тестовые модули при импорте подхватывают .env (dotenv) и
выставляют BINANCE_TESTNET=true. Источник веса gate читается в момент создания
объекта, поэтому без явного значения тесты header-режима зависели бы от порядка
импорта. По умолчанию фиксируем header (историческое поведение); тесты
локального счётчика выставляют BINANCE_WEIGHT_SOURCE явно.
"""
import pytest


@pytest.fixture(autouse=True)
def _default_weight_source(monkeypatch):
    monkeypatch.setenv("BINANCE_WEIGHT_SOURCE", "header")
