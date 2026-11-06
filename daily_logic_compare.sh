#!/bin/bash
cd /opt/trading-bot
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY'
import sqlite3
con = sqlite3.connect('/app/data/bot.db'); con.row_factory = sqlite3.Row
print("=== брутто-убыток (текущая логика) vs нетто-PnL (предлагаемая) ===")
for r in con.execute("""
SELECT substr(exit_time,1,10) d,
       COUNT(*) n,
       ROUND(SUM(CASE WHEN pnl<0 THEN -pnl ELSE 0 END),4) gross_loss,
       ROUND(SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END),4) gross_win,
       ROUND(SUM(pnl),4) net,
       ROUND(COALESCE(SUM(commission),0),4) comm
FROM trades WHERE is_open=0 AND exit_time >= '2026-11-01'
GROUP BY d ORDER BY d"""):
    print(dict(r))

print("\n=== пример: сколько раз брутто-лимит сработал бы раньше нетто ===")
for lim in (0.5, 1.0, 3.0):
    print(f"лимит ${lim}:")
    for r in con.execute("""
    SELECT substr(exit_time,1,10) d,
           ROUND(SUM(CASE WHEN pnl<0 THEN -pnl ELSE 0 END),4) gl,
           ROUND(SUM(pnl),4) net
    FROM trades WHERE is_open=0 AND exit_time >= '2026-11-01'
    GROUP BY d ORDER BY d"""):
        print(f"  {r['d']}: брутто={r['gl']:+.4f} -> {'СТОП' if r['gl']>=lim else 'торгуем'}"
              f" | нетто={r['net']:+.4f} -> {'СТОП' if r['net']<=-lim else 'торгуем'}")
PY
