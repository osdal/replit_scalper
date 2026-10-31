#!/bin/bash
echo "=== Комиссии vs PnL (7 дней, реальные сделки) ==="
python3 - <<'PY'
import sqlite3
con=sqlite3.connect("file:/opt/trading-bot/data/bot.db?mode=ro", uri=True)
c=con.cursor()
q="""SELECT count(*), round(sum(pnl),4), round(sum(coalesce(commission,0)),4),
round(avg(pnl),4), round(avg(coalesce(commission,0)),4),
sum(CASE WHEN pnl>0 THEN 1 ELSE 0 END)
FROM trades WHERE is_open=0 AND status!='rejected' AND exit_time>=datetime('now','-7 day')"""
print("trades,sum_pnl,sum_comm,avg_pnl,avg_comm,wins:", list(c.execute(q))[0])
q2="""SELECT round(sum(coalesce(commission,0))/nullif(sum(abs(pnl)),0)*100,1)
FROM trades WHERE is_open=0 AND status!='rejected' AND exit_time>=datetime('now','-7 day')"""
print("commission as % of gross |pnl|:", list(c.execute(q2))[0][0])
print()
print("=== pnl по exit_reason 30-31.10 ===")
for r in c.execute("""SELECT exit_reason,count(*),round(sum(pnl),3),round(sum(coalesce(commission,0)),3)
FROM trades WHERE is_open=0 AND status!='rejected' AND exit_time>='2026-10-30'
GROUP BY 1 ORDER BY 2 DESC"""): print(r)
print()
print("=== размер нотионала последних сделок (qty*entry) ===")
for r in c.execute("""SELECT symbol, round(qty*entry_price,2) notional, round(pnl,3), exit_reason
FROM trades WHERE is_open=0 AND status!='rejected' ORDER BY exit_time DESC LIMIT 12"""): print(r)
PY
echo
echo "=== stale_close логика ==="
grep -rn "stale_close" /opt/trading-bot/bot/*.py | head -10
echo
echo "=== когда max_positions стало 1 (git log) ==="
cd /opt/trading-bot && git log --oneline -5 -- bot/recovery_config.yaml
