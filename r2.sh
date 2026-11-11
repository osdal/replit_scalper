#!/bin/bash
date -u
echo "=== distinct statuses (since Nov 7) ==="
python3 - << 'EOF'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db')
print(c.execute("select status, count(*) from trades where entry_time >= '2026-11-07 14:00:00' group by status").fetchall())
print("modes:", c.execute("select mode, count(*) from trades where entry_time >= '2026-11-07 14:00:00' group by mode").fetchall())
EOF
echo
echo "=== closed trades since 2026-11-07 14:00 UTC (real only) ==="
python3 - << 'EOF'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db'); c.row_factory = sqlite3.Row
q = """
select symbol, count(*) n,
       sum(case when pnl > 0 then 1 else 0 end) w,
       round(sum(pnl),4) pnl,
       round(min(pnl),4) worst,
       round(max(pnl),4) best
from trades
where entry_time >= '2026-11-07 14:00:00'
  and is_open = 0 and exit_time is not null
  and (status is null or status not in ('rejected','virtual'))
group by symbol order by pnl desc
"""
rows = c.execute(q).fetchall()
print(f"{'symbol':13}{'n':>4}{'win%':>7}{'pnl':>10}{'worst':>10}{'best':>9}")
tn = tw = 0; tp = 0.0
for r in rows:
    wr = 100.0*r['w']/r['n'] if r['n'] else 0
    print(f"{r['symbol']:13}{r['n']:4d}{wr:7.1f}{r['pnl']:10.4f}{r['worst']:10.4f}{r['best']:9.4f}")
    tn += r['n']; tw += r['w']; tp += r['pnl'] or 0
print(f"{'TOTAL':13}{tn:4d}{(100.0*tw/tn if tn else 0):7.1f}{tp:10.4f}")
EOF
echo
echo "=== by UTC day ==="
python3 - << 'EOF'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db'); c.row_factory = sqlite3.Row
q = """
select substr(entry_time,1,10) d, count(*) n,
       sum(case when pnl > 0 then 1 else 0 end) w,
       round(sum(pnl),4) pnl
from trades
where entry_time >= '2026-11-07 14:00:00' and is_open = 0 and exit_time is not null
  and (status is null or status not in ('rejected','virtual'))
group by d order by d
"""
for r in c.execute(q):
    wr = 100.0*r['w']/r['n'] if r['n'] else 0
    print(f"{r['d']}  n={r['n']:3d}  win={wr:5.1f}%  pnl={r['pnl']:+.4f}")
EOF
echo
echo "=== open positions now ==="
python3 - << 'EOF'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db')
for r in c.execute("select symbol, direction, round(entry_price,6), qty, datetime(entry_time), status from trades where is_open=1"):
    print(r)
EOF
echo
echo "=== exit reasons ==="
python3 - << 'EOF'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db')
for r in c.execute("""select coalesce(exit_reason,'?') e, count(*) n, round(sum(pnl),3)
                      from trades where entry_time >= '2026-11-07 14:00:00' and is_open=0
                        and (status is null or status not in ('rejected','virtual'))
                      group by e order by n desc"""):
    print(f"{r[0]:28} {r[1]:4d} {r[2]:+.3f}")
EOF
