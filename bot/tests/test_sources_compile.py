"""Гард от «мёртвого main»: любой синтаксически битый файл бота ловится в CI/локально.

Запуск:  cd bot && python -m pytest tests/test_sources_compile.py -q
"""
import glob
import os

import pytest

BOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = sorted(glob.glob(os.path.join(BOT_DIR, "*.py")))


@pytest.mark.parametrize("path", FILES, ids=[os.path.basename(p) for p in FILES])
def test_bot_source_compiles(path):
    # Байты, а не текст: так читает сам интерпретатор (BOM и coding-cookie корректны).
    with open(path, "rb") as f:
        src = f.read()
    compile(src, path, "exec")  # SyntaxError/IndentationError -> тест падает
