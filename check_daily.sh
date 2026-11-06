#!/bin/bash
cd /opt/trading-bot
echo "=== текущий день: закрытые сделки ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT symbol,direction,qty,entry_price,exit_price,pnl,exit_reason,entry_time,exit_time
FROM trades WHERE is_open=0 AND exit_time >= date('now') ORDER BY exit_time"""):
    print(f"{r['symbol']:<9} {r['direction']:<5} qty={r['qty']:>8} entry={r['entry_price']:>8} exit={r['exit_price']:>8} pnl={r['pnl']:>+8.4f} reason={r['exit_reason']}")
print()
for r in con.execute("""SELECT 
    SUM(CASE WHEN pnl<0 THEN -pnl ELSE 0 END) gross_loss,
    SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END) gross_win,
    SUM(pnl) net,
    COUNT(*) n
FROM trades WHERE is_open=0 AND exit_time >= date('now')"""):
    print(f"грубый убыток: {r['gross_loss']:.4f} | грубая прибыль: {r['gross_win']:.4f} | нетто: {r['net']:.4f} | сделок: {r['n']}")
PY

echo
echo "=== текущий день: EXPIRED-закрытия ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT symbol,direction,qty,entry_price,exit_price,pnl,exit_reason,exit_time
FROM trades WHERE is_open=0 AND exit_reason IN ('EXPIRED_CLOSE','REVERSE_EXPIRED_PROFIT') AND exit_time >= date('now')
ORDER BY exit_time"""):
    print(f"{r['symbol']:<9} {r['direction']:<5} pnl={r['pnl']:+.4f} reason={r['exit_reason']} time={r['exit_time'][:19]}")
PY