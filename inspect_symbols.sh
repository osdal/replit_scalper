#!/bin/bash
date -u
echo "=== bots: is_running ==="
python3 << 'EOF'
import sqlite3
c=sqlite3.connect("/opt/trading-bot/data/bot.db"); c.row_factory=sqlite3.Row
rows=c.execute("select symbol,is_running,stop_requested from bots order by symbol").fetchall()
print("total:", len(rows))
run=[r["symbol"] for r in rows if r["is_running"]]
print("running:", len(run))
print(", ".join(run))
EOF
echo ""
echo "=== stats per symbol: last 14 days (closed trades, non-rejected) ==="
python3 << 'EOF'
import sqlite3
c=sqlite3.connect("/opt/trading-bot/data/bot.db"); c.row_factory=sqlite3.Row
q="""
select symbol,
       count(*) n,
       sum(case when pnl>0 then 1 else 0 end) wins,
       round(sum(pnl),4) pnl
from trades
where is_open=0
  and (status is null or status not in ('rejected','virtual'))
  and exit_time is not null
  and entry_time > datetime('now','-14 days')
group by symbol
having n >= 5
order by pnl desc
"""
rows=c.execute(q).fetchall()
print(f"{'symbol':14} {'n':>4} {'win%':>6} {'pnl':>9}")
for r in rows:
    wr = 100.0*r['wins']/r['n'] if r['n'] else 0
    print(f"{r['symbol']:14} {r['n']:4d} {wr:6.1f} {r['pnl']:9.3f}")
EOF
echo ""
echo "=== open positions symbols ==="
python3 -c "import sqlite3;c=sqlite3.connect('/opt/trading-bot/data/bot.db');print([r[0] for r in c.execute('select distinct symbol from trades where is_open=1')])"
echo ""
echo "=== how bots are launched on container start ==="
ls -1 /opt/trading-bot/*.sh 2>/dev/null | head -20
echo "--- compose command/entrypoint ---"
grep -n -A3 -E "command:|entrypoint:" /opt/trading-bot/docker-compose.yml | head -40
echo "--- autostart script references ---"
grep -rn "configs/testnet" /opt/trading-bot/artifacts/api-server/src/index.ts /opt/trading-bot/artifacts/api-server/src/routes/bots.ts 2>/dev/null | head -10
