"""
Парсит лог файлы ботов и импортирует сделки в дашборд через API.
Запуск: python log_importer.py
Можно запускать периодически или один раз для импорта истории.
"""
import re
import os
import sys
import json
import glob
import requests
from datetime import datetime
from typing import Optional

API_URL = os.getenv("DASHBOARD_API_URL", "http://localhost:5000/api")

# Паттерны для парсинга лога
RE_TIMESTAMP = r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
RE_SYMBOL    = r"\[(?:LIVE|PAPER) (\w+USDT)\]"
RE_MODE      = r"\[(LIVE|PAPER) \w+USDT\]"

RE_OPEN = re.compile(
    RE_TIMESTAMP + r".*?" + RE_SYMBOL + r".*?Position opened \| (LONG|SHORT) \| "
    r"entry=([\d.]+) SL=([\d.]+) TP1=([\d.]+) TP2=([\d.]+) qty=([\d.]+)"
    r".*?ema_fast=([\d.]+) ema_slow=([\d.]+) volume=([\d.]+) volume_ma=([\d.]+)"
)

RE_TP1 = re.compile(
    RE_TIMESTAMP + r".*?" + RE_SYMBOL + r".*?TP1 hit \| price=([\d.]+) "
    r"closed_qty=([\d.]+) remaining_qty=([\d.]+) pnl=([\d.-]+)"
)

RE_TP2 = re.compile(
    RE_TIMESTAMP + r".*?" + RE_SYMBOL + r".*?TP2 hit \| price=([\d.]+) "
    r"qty=([\d.]+) pnl=([\d.-]+) total_pnl=([\d.-]+)"
)

RE_SL = re.compile(
    RE_TIMESTAMP + r".*?" + RE_SYMBOL + r".*?SL hit.*?\| price=([\d.]+) "
    r"qty=([\d.]+) pnl=([\d.-]+)"
)


def parse_log(filepath: str) -> list[dict]:
    """Парсит один лог файл и возвращает список сделок."""
    trades = []
    open_trades: dict[str, dict] = {}  # "symbol_entry_time" -> trade

    # Определяем mode из имени файла
    mode = "live" if "eth" in filepath.lower() else "paper"

    try:
        with open(filepath, encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
    except Exception as e:
        print(f"  Error reading {filepath}: {e}")
        return []

    for line in lines:
        # Position opened
        m = RE_OPEN.search(line)
        if m:
            ts, symbol, direction, entry, sl, tp1, tp2, qty = m.group(1,2,3,4,5,6,7,8)
            ema_fast, ema_slow, volume, volume_ma = m.group(9,10,11,12)
            key = f"{symbol}_{ts.replace(' ', 'T')}"
            open_trades[key] = {
                "symbol": symbol,
                "direction": direction,
                "entry_price": float(entry),
                "sl_price": float(sl),
                "tp1_price": float(tp1),
                "tp2_price": float(tp2),
                "qty": float(qty),
                "entry_time": ts.replace(" ", "T"),
                "is_open": True,
                "mode": mode,
                "ema_fast": float(ema_fast),
                "ema_slow": float(ema_slow),
                "volume": float(volume),
                "volume_ma": float(volume_ma),
                "pnl": 0.0,
                "tp1_hit": False,
            }
            continue

        # TP1 hit - ищем по symbol, берём последнюю открытую позицию
        m = RE_TP1.search(line)
        if m:
            ts, symbol, price, closed_qty, remaining_qty, pnl = m.group(1,2,3,4,5,6)
            # Находим ВСЕ открытые позиции по symbol и берём последнюю (по времени)
            matching_keys = [k for k in open_trades.keys() if k.startswith(f"{symbol}_")]
            if matching_keys:
                key = sorted(matching_keys)[-1]  # последняя по времени
                t = open_trades[key]
                t["tp1_hit"] = True
                t["pnl"] = float(pnl)
                t["qty"] = float(closed_qty)
                # Записываем частичное закрытие по TP1
                tp1_trade = {**t, "exit_price": float(price), "exit_time": ts.replace(" ", "T"),
                             "exit_reason": "TP1", "is_open": False, "pnl": float(pnl)}
                trades.append(tp1_trade)
                # Обновляем остаток
                t["qty"] = float(remaining_qty)
                t["pnl"] = 0.0
            continue

        # TP2 hit - ищем по symbol
        m = RE_TP2.search(line)
        if m:
            ts, symbol, price, qty, pnl, total_pnl = m.group(1,2,3,4,5,6)
            matching_keys = [k for k in open_trades.keys() if k.startswith(f"{symbol}_")]
            if matching_keys:
                key = sorted(matching_keys)[-1]
                t = open_trades.pop(key)
                trades.append({**t, "exit_price": float(price), "exit_time": ts.replace(" ", "T"),
                               "exit_reason": "TP2", "is_open": False, "pnl": float(pnl),
                               "qty": float(qty)})
            continue

        # SL hit - ищем по symbol
        m = RE_SL.search(line)
        if m:
            ts, symbol, price, qty, pnl = m.group(1,2,3,4,5)
            matching_keys = [k for k in open_trades.keys() if k.startswith(f"{symbol}_")]
            if matching_keys:
                key = sorted(matching_keys)[-1]
                t = open_trades.pop(key)
                trades.append({**t, "exit_price": float(price), "exit_time": ts.replace(" ", "T"),
                               "exit_reason": "SL", "is_open": False, "pnl": float(pnl),
                               "qty": float(qty)})
            continue

    # Добавляем незакрытые позиции как открытые сделки
    for key, t in open_trades.items():
        trades.append(t)

    return trades


def get_existing_trades() -> set[str]:
    """Получает уже импортированные сделки (entry_time + symbol)."""
    try:
        r = requests.get(f"{API_URL}/trades", params={"limit": 1000}, timeout=10)
        data = r.json()
        trades = data.get("trades", [])
        return {f"{t['symbol']}_{t['entry_time']}" for t in trades}
    except Exception as e:
        print(f"  Warning: could not fetch existing trades: {e}")
        return set()


def import_trades(trades: list[dict], existing: set[str]) -> int:
    """Отправляет новые сделки в API."""
    imported = 0
    for trade in trades:
        key = f"{trade['symbol']}_{trade['entry_time']}"
        if key in existing:
            continue
        try:
            r = requests.post(f"{API_URL}/trades", json=trade, timeout=10)
            if r.status_code in (200, 201):
                imported += 1
            else:
                print(f"  Warning: POST /trades returned {r.status_code}: {r.text[:100]}")
        except Exception as e:
            print(f"  Error posting trade: {e}")
    return imported


def main():
    bot_dir = os.getenv("BOT_DIR", os.path.join(os.path.dirname(__file__)))
    logs_dir = os.path.join(bot_dir, "logs")

    print(f"Scanning logs in: {logs_dir}")

    log_files = glob.glob(os.path.join(logs_dir, "*.log"))
    if not log_files:
        print("No log files found")
        return

    existing = get_existing_trades()
    print(f"Existing trades in DB: {len(existing)}")

    total_imported = 0
    for filepath in log_files:
        print(f"\nParsing: {os.path.basename(filepath)}")
        trades = parse_log(filepath)
        print(f"  Found {len(trades)} trades")
        if trades:
            n = import_trades(trades, existing)
            print(f"  Imported: {n} new trades")
            total_imported += n

    print(f"\nTotal imported: {total_imported}")


if __name__ == "__main__":
    main()
