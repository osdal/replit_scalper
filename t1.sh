#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== schema ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
print([r[1] for r in con.execute("PRAGMA table_info(trades)")])
PY
echo
echo "=== exit_reason counts, last 5 days (non-rejected) ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
q = """
SELECT substr(exit_time,1,10) d, exit_reason, COUNT(*) n, ROUND(SUM(pnl),4) pnl,
       ROUND(MIN(pnl),4) worst, ROUND(MAX(pnl),4) best, MAX(chain_depth) maxchain
FROM trades
WHERE status!='rejected' AND exit_time IS NOT NULL AND exit_time >= '2026-11-05'
GROUP BY d, exit_reason
ORDER BY d, n DESC
"""
for r in con.execute(q):
    print(r)
PY
echo
echo "=== SL / non-reverse exits, last 5 days (detail) ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
q = """
SELECT id, symbol, direction, ROUND(entry_price,6) ep, ROUND(exit_price,6) xp,
       ROUND(pnl,4) pnl, ROUND(cycle_pnl,4) cyc, chain_depth, is_open,
       exit_reason, entry_time, exit_time
FROM trades
WHERE status!='rejected' AND exit_time >= '2026-11-05'
  AND (exit_reason LIKE '%SL%' OR exit_reason LIKE '%BACKSTOP%' OR exit_reason IS NULL)
ORDER BY id DESC LIMIT 40
"""
for r in con.execute(q):
    print(r)
PY
