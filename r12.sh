#!/bin/bash
date -u
python3 - << 'EOF'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db'); c.row_factory = sqlite3.Row
print("=== PnL by chain_depth (closed since Nov 7 14:00) ===")
for r in c.execute("""select coalesce(chain_depth,0) cd, count(*) n, round(sum(pnl),4) s, round(avg(pnl),4) a
                      from trades where entry_time >= '2026-11-07 14:00:00' and is_open=0
                        and (status is null or status not in ('rejected','virtual'))
                      group by cd order by cd"""):
    print(f"chain={r['cd']}  n={r['n']:3d}  sum={r['s']:+8.4f}  avg={r['a']:+8.4f}")
print()
print("=== LSK -0.6032 details ===")
for r in c.execute("""select id, symbol, direction, qty, round(entry_price,6) ep, round(exit_price,6) xp,
                             round(pnl,4) pnl, round(commission,4) comm, exit_reason, chain_depth,
                             datetime(entry_time), datetime(exit_time), cycle_pnl, leg_pnl
                      from trades where pnl < -0.5 and entry_time >= '2026-11-07 14:00:00'"""):
    print(dict(r))
print()
print("=== total commission since Nov 7 14:00 ===")
r = c.execute("""select round(sum(commission),4), round(sum(pnl),4) from trades
                 where entry_time >= '2026-11-07 14:00:00' and is_open=0
                   and (status is null or status not in ('rejected','virtual'))""").fetchone()
print("commission:", r[0], "pnl:", r[1])
EOF
