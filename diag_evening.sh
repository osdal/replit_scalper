#!/bin/bash
cd /opt/trading-bot
echo "=== -2022 за 03.11 (строки) ==="
grep -h "\-2022" bot/logs/testnet/api_*.log 2>/dev/null | grep "2026-11-03" | tail -15
echo
echo "=== -1003 после 11:40 ==="
grep -h "\-1003" bot/logs/testnet/api_*.log 2>/dev/null | grep -E "2026-11-03 (1[2-9]|2[0-3]):" | tail -12
echo
echo "=== TP replace budget сработал? ==="
grep -hE "TP_REPLACE|tp replace|replace budget|aggressive market" bot/logs/testnet/api_*.log 2>/dev/null | grep "2026-11-03" | tail -10
echo
echo "=== EXPIRED закрытия за 03.11 ==="
grep -h "\[EXPIRED\] position in profit" bot/logs/testnet/api_*.log 2>/dev/null | grep "2026-11-03" | tail -10
echo
echo "=== Протухшие сейчас: почему не закрыты (последние проверки) ==="
for s in dogeusdt aptusdt linkusdt nearusdt; do
  echo "--- $s"
  grep -hE "\[EXPIRED\]" bot/logs/testnet/api_${s}_stdout.log 2>/dev/null | tail -3
done
echo
echo "=== PnL по exit_reason за 03.11 (только \$10-режим, без легаси) ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
rows = con.execute("""SELECT exit_reason, COUNT(*) n, ROUND(SUM(pnl),4) pnl
FROM trades WHERE exit_time >= '2026-11-03' AND is_open=0 AND status!='rejected'
GROUP BY exit_reason ORDER BY n DESC""").fetchall()
for r in rows: print(dict(r))
print()
print("--- $10-режим (entry_time >= 2026-11-03T11:00), закрытые:")
rows = con.execute("""SELECT symbol, exit_reason, ROUND(pnl,4) pnl, ROUND(commission,6) commission,
ROUND(qty*entry_price,2) notional, ROUND(chain_depth,0) chain, entry_time, exit_time
FROM trades WHERE is_open=0 AND status!='rejected' AND entry_time >= '2026-11-03T11:00'
ORDER BY exit_time""").fetchall()
for r in rows: print(dict(r))
print("итого:", len(rows), "pnl=", round(sum(r['pnl'] or 0 for r in rows),4),
      "wins:", sum(1 for r in rows if (r['pnl'] or 0) > 0),
      "losses:", sum(1 for r in rows if (r['pnl'] or 0) < 0),
      "commissions:", round(sum(r['commission'] or 0 for r in rows),4))
PY
