#!/bin/bash
date -u
python3 - << 'EOF'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db')
print("=== last 6 closed trades ===")
for r in c.execute("""select symbol, round(pnl,4), exit_reason, datetime(exit_time)
                      from trades where is_open=0 and entry_time >= '2026-11-09 00:00:00'
                        and (status is null or status not in ('rejected','virtual'))
                      order by exit_time desc limit 6"""):
    print(r)
print()
print("=== trades per day for kept 8 only ===")
q = """select substr(entry_time,1,10), count(*), round(sum(pnl),4)
       from trades where entry_time >= '2026-11-07 14:00:00' and is_open=0
         and symbol in ('ADAUSDT','FILUSDT','INJUSDT','NEARUSDT','TRXUSDT','DOGEUSDT','1000PEPEUSDT','LSKUSDT')
         and (status is null or status not in ('rejected','virtual'))
       group by 1 order by 1"""
for r in c.execute(q):
    print(r)
EOF
