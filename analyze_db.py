#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Статистика из bot.db: сделки, причины закрытия, циклы, глубина цепочки."""
import sqlite3, datetime, collections

DB = "/opt/trading-bot/data/bot.db"
con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
cur = con.cursor()

print("=== Таблицы ===")
for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"):
    print(" ", r[0])

def cols(t):
    try:
        return [c[1] for c in cur.execute(f"PRAGMA table_info({t})")]
    except Exception:
        return []

for t in ("trades", "positions", "cycles", "bot_control"):
    print(f"\n--- {t} columns ---")
    print(", ".join(cols(t)))

print("\n=== trades: агрегаты по дням (закрытые) ===")
try:
    q = """
    SELECT substr(exit_time,1,10) d,
           count(*) n,
           sum(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins,
           round(sum(pnl),4) pnl,
           round(avg(pnl),4) avg,
           round(min(pnl),4) worst,
           round(max(pnl),4) best
    FROM trades
    WHERE is_open=0 AND exit_time IS NOT NULL
    GROUP BY d ORDER BY d DESC LIMIT 14
    """
    print(f"{'date':12}{'n':>6}{'wins':>6}{'pnl':>10}{'avg':>9}{'worst':>9}{'best':>9}")
    for r in cur.execute(q):
        print(f"{r['d']:12}{r['n']:>6}{r['wins']:>6}{r['pnl']:>10.4f}{r['avg']:>9.4f}{r['worst']:>9.4f}{r['best']:>9.4f}")
except Exception as e:
    print("ERR", e)

print("\n=== trades: причины закрытия за 7 дней ===")
try:
    q = """
    SELECT exit_reason, count(*) n, round(sum(pnl),4) pnl, round(avg(pnl),4) avg,
           sum(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins
    FROM trades
    WHERE is_open=0 AND exit_time >= datetime('now','-7 day')
    GROUP BY exit_reason ORDER BY n DESC
    """
    print(f"{'reason':24}{'n':>6}{'wins':>6}{'pnl':>10}{'avg':>9}")
    for r in cur.execute(q):
        print(f"{str(r['exit_reason'])[:24]:24}{r['n']:>6}{r['wins'] or 0:>6}{r['pnl'] or 0:>10.4f}{r['avg'] or 0:>9.4f}")
except Exception as e:
    print("ERR", e)

print("\n=== trades: chain_depth распределение (7 дней) ===")
try:
    q = """
    SELECT chain_depth, count(*) n, round(sum(pnl),4) pnl, round(avg(pnl),4) avg
    FROM trades WHERE is_open=0 AND exit_time >= datetime('now','-7 day')
    GROUP BY chain_depth ORDER BY chain_depth
    """
    for r in cur.execute(q):
        print(f"depth={r['chain_depth']} n={r['n']} pnl={r['pnl']} avg={r['avg']}")
except Exception as e:
    print("ERR", e)

print("\n=== Открытые позиции ===")
try:
    for r in cur.execute("SELECT symbol, direction, entry_price, qty, entry_time, chain_depth FROM trades WHERE is_open=1"):
        print(dict(r))
except Exception as e:
    print("ERR", e)

print("\n=== Сделки за последние 3 дня (детально) ===")
try:
    q = """
    SELECT symbol, direction, entry_time, exit_time, round(entry_price,6) ep, round(exit_price,6) xp,
           qty, round(pnl,4) pnl, exit_reason, status, chain_depth, cycle_close_reason
    FROM trades WHERE exit_time >= datetime('now','-3 day') ORDER BY exit_time DESC LIMIT 40
    """
    for r in cur.execute(q):
        print(f"{r['exit_time']} {r['symbol']:12} {r['direction']:5} {r['ep']}->{r['xp']} pnl={r['pnl']} {r['exit_reason']} depth={r['chain_depth']} cyc={r['cycle_close_reason']}")
except Exception as e:
    print("ERR", e)
