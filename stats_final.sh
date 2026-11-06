#!/bin/bash
cd /opt/trading-bot
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY'
import sqlite3
con = sqlite3.connect('/app/data/bot.db'); con.row_factory = sqlite3.Row

print("=== по дням (с 04.11) ===")
for r in con.execute("""SELECT substr(exit_time,1,10) d, COUNT(*) n,
SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) w, ROUND(SUM(pnl),4) pnl,
ROUND(COALESCE(SUM(commission),0),4) comm
FROM trades WHERE is_open=0 AND exit_time >= '2026-11-04' GROUP BY d ORDER BY d"""):
    print(dict(r))

print("\n=== все закрытия с 04.11 00:00 ===")
rows = con.execute("""SELECT id,symbol,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,
ROUND(exit_price,6) exit,ROUND(pnl,4) pnl,ROUND(COALESCE(commission,0),4) comm,
exit_reason,exit_time FROM trades WHERE is_open=0 AND exit_time >= '2026-11-04'
ORDER BY exit_time""").fetchall()
tot = 0.0; wins = 0; comm = 0.0
for r in rows:
    p = float(r['pnl'] or 0); tot += p; wins += 1 if p > 0 else 0; comm += float(r['comm'] or 0)
    print(f"{r['exit_time'][:16]} {r['symbol']:<9} {r['direction']:<5} pnl={p:+.4f} fee={float(r['comm'] or 0):.4f} {r['exit_reason']}")
print(f"\nитог: {len(rows)} сделок | {wins} в плюс ({wins/len(rows)*100:.0f}%) | PnL={tot:+.4f} | комиссии={comm:.4f} | net={tot-comm:+.4f}")

print("\n=== открытые сейчас ===")
for r in con.execute("""SELECT id,symbol,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,entry_time
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY symbol"""):
    print(dict(r))

print("\n=== daily_loss по дням ===")
for r in con.execute("""SELECT substr(exit_time,1,10) d, ROUND(COALESCE(SUM(-pnl),0),4) loss
FROM trades WHERE is_open=0 AND pnl<0 AND exit_time >= '2026-11-03' GROUP BY d ORDER BY d"""):
    print(dict(r))
con.close()
PY
