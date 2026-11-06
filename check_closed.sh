#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
since = '2026-10-31T14:00:00'
print("=== Закрытия ПОСЛЕ деплоя (по exit_time), accepted ===")
cur = con.execute("""
SELECT symbol, exit_reason, ROUND(pnl,4) pnl, ROUND(qty*entry_price,2) notional,
       entry_time, exit_time
FROM trades WHERE exit_time >= ? AND is_open=0 AND status != 'rejected'
ORDER BY exit_time
""", (since,))
rows = cur.fetchall()
for r in rows: print(dict(r))
print("итого:", len(rows), "pnl=", round(sum(r['pnl'] or 0 for r in rows),4))
print()
print("=== Свод по exit_reason (exit_time после деплоя) ===")
cur = con.execute("""
SELECT exit_reason, COUNT(*) n, ROUND(SUM(pnl),4) pnl
FROM trades WHERE exit_time >= ? AND is_open=0 AND status != 'rejected'
GROUP BY exit_reason ORDER BY n DESC
""", (since,))
for r in cur.fetchall(): print(dict(r))
print()
print("=== Открытые: оценка плавающего PnL по последним ценам из логов ===")
PX = {'ATOMUSDT':1.42,'DOGEUSDT':0.07437,'MTLUSDT':0.26}
cur = con.execute("SELECT symbol,direction,qty,entry_price,pnl,entry_time FROM trades WHERE is_open=1 AND status!='rejected'")
for r in cur.fetchall():
    px = PX.get(r['symbol'])
    sign = 1 if r['direction']=='LONG' else -1
    upnl = (px-r['entry_price'])*r['qty']*sign if px else None
    print(r['symbol'], r['direction'], f"entry={r['entry_price']}", f"px={px}",
          f"upnl={upnl:+.4f}" if upnl is not None else "upnl=?",
          f"stored_pnl={r['pnl']}")
PY
