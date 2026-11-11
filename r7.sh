#!/bin/bash
date -u
echo "=== worst 8 trades ==="
python3 - << 'EOF'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db'); c.row_factory = sqlite3.Row
q = """select symbol, direction, round(entry_price,6) ep, round(exit_price,6) xp,
              round(pnl,4) pnl, exit_reason, datetime(entry_time) et, datetime(exit_time) xt,
              status, chain_depth, cycle_close_reason
       from trades where entry_time >= '2026-11-07 14:00:00' and is_open=0
         and (status is null or status not in ('rejected','virtual'))
       order by pnl asc limit 8"""
for r in c.execute(q):
    print(f"{r['symbol']:12} {r['direction']:5} {r['ep']:>12} -> {r['xp']:>12} "
          f"{r['pnl']:+8.4f} {str(r['exit_reason'])[:24]:24} {r['et']} -> {r['xt']} "
          f"chain={r['chain_depth']} cyc={r['cycle_close_reason']}")
EOF
echo
echo "=== PnL by exit_reason (all 49) ==="
python3 - << 'EOF'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db')
for r in c.execute("""select coalesce(exit_reason,'?'), count(*), round(sum(pnl),4), round(avg(pnl),4)
                      from trades where entry_time >= '2026-11-07 14:00:00' and is_open=0
                        and (status is null or status not in ('rejected','virtual'))
                      group by 1 order by 3 asc"""):
    print(f"{str(r[0]):28} n={r[1]:3d} sum={r[2]:+8.4f} avg={r[3]:+8.4f}")
EOF
echo
echo "=== rejected per symbol (Nov 8-10) ==="
python3 - << 'EOF'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db')
for r in c.execute("""select symbol, count(*) n from trades
                      where entry_time >= '2026-11-08 00:00:00' and status='rejected'
                      group by symbol order by n desc"""):
    print(f"{r[0]:13} {r[1]}")
EOF
echo
echo "=== avg holding time (closed) ==="
python3 - << 'EOF'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db')
r = c.execute("""select round(avg((julianday(exit_time)-julianday(entry_time))*24*60),1),
                        min((julianday(exit_time)-julianday(entry_time))*24*60),
                        max((julianday(exit_time)-julianday(entry_time))*24*60)
                 from trades where entry_time >= '2026-11-07 14:00:00' and is_open=0
                   and (status is null or status not in ('rejected','virtual'))""").fetchone()
print(f"avg={r[0]} min={round(r[1],1)} max={round(r[2],1)} minutes")
EOF
