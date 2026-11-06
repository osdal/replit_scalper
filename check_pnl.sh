#!/bin/bash
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3
con = sqlite3.connect("data/bot.db"); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT 
    SUM(CASE WHEN pnl<0 THEN -pnl ELSE 0 END) gross_loss,
    SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END) gross_win,
    SUM(pnl) net,
    COUNT(*) n
FROM trades WHERE is_open=0 AND exit_time >= date('now')"""):
    print(f"gross_loss: {r['gross_loss']:.4f} | gross_win: {r['gross_win']:.4f} | net: {r['net']:.4f} | trades: {r['n']}")
PY