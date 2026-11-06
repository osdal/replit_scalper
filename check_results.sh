#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
since = '2026-10-31T14:00:00'

print("=== 1. Сделки с момента деплоя (все) ===")
cur = con.execute("""
SELECT status, COUNT(*) n,
       ROUND(SUM(CASE WHEN is_open=0 THEN pnl ELSE 0 END),4) closed_pnl,
       ROUND(SUM(CASE WHEN is_open=0 AND pnl>0 THEN 1 ELSE 0 END),0) wins,
       ROUND(SUM(CASE WHEN is_open=0 AND pnl<0 THEN 1 ELSE 0 END),0) losses
FROM trades WHERE entry_time >= ? GROUP BY status
""", (since,))
for r in cur.fetchall(): print(dict(r))

print()
print("=== 2. Exit reasons (закрытые, accepted) ===")
cur = con.execute("""
SELECT exit_reason, COUNT(*) n, ROUND(SUM(pnl),4) pnl,
       ROUND(AVG(pnl),4) avg
FROM trades WHERE entry_time >= ? AND is_open=0 AND status != 'rejected'
GROUP BY exit_reason ORDER BY n DESC
""", (since,))
for r in cur.fetchall(): print(dict(r))

print()
print("=== 3. PnL по дням (accepted, closed) ===")
cur = con.execute("""
SELECT substr(entry_time,1,10) d, COUNT(*) n,
       ROUND(SUM(pnl),4) pnl,
       SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins,
       SUM(CASE WHEN pnl<0 THEN 1 ELSE 0 END) losses
FROM trades WHERE entry_time >= ? AND is_open=0 AND status != 'rejected'
GROUP BY d ORDER BY d
""", (since,))
for r in cur.fetchall(): print(dict(r))

print()
print("=== 4. Открытые позиции сейчас ===")
cur = con.execute("""
SELECT symbol, direction, ROUND(qty,6) qty, ROUND(entry_price,6) entry,
       ROUND(entry_price*qty,2) notional,
       ROUND((julianday('now')-julianday(entry_time))*24,1) age_h,
       status
FROM trades WHERE is_open=1 AND status != 'rejected' ORDER BY entry_time
""")
for r in cur.fetchall(): print(dict(r))

print()
print("=== 5. Rejected: причины отказа (с деплоя) ===")
cur = con.execute("""
SELECT reject_reason, COUNT(*) n FROM trades
WHERE entry_time >= ? AND status='rejected' GROUP BY reject_reason ORDER BY n DESC
""", (since,))
for r in cur.fetchall(): print(dict(r))

print()
print("=== 6. Notional/маржa реальных входов (min/max/avg) ===")
cur = con.execute("""
SELECT COUNT(*) n, ROUND(MIN(qty*entry_price),2) min_notional,
       ROUND(MAX(qty*entry_price),2) max_notional,
       ROUND(AVG(qty*entry_price),2) avg_notional
FROM trades WHERE entry_time >= ? AND status != 'rejected'
""", (since,))
for r in cur.fetchall(): print(dict(r))
PY
