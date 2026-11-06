#!/bin/bash
cd /opt/trading-bot
echo "=== колонки trades ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
cols = [r['name'] for r in con.execute("PRAGMA table_info(trades)")]
print(cols)
print()
print("=== ETH / ADA / WLD открытые строки ===")
for r in con.execute("""SELECT * FROM trades WHERE is_open=1 AND status!='rejected'
AND symbol IN ('ETHUSDT','ADAUSDT','WLDUSDT','SUIUSDT')"""):
    d = dict(r)
    print({k: d[k] for k in d if k in ('id','symbol','direction','qty','entry_price','entry_time','status','preset','is_recovery','recovery_chain_id','cycle_id','tp1','tp2','reason','exit_reason')})
PY
echo
echo "=== ETH: все строки за 04-05.11 ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,direction,qty,entry_price,exit_price,pnl,exit_reason,entry_time,exit_time,is_open
FROM trades WHERE symbol='ETHUSDT' AND entry_time >= '2026-11-04' ORDER BY entry_time"""):
    print(dict(r))
PY
echo
echo "=== ETH логи: EXPIRED / reverse / cum ==="
grep -hE '\[EXPIRED\]|reverse=True|CUM_LOSS|REVERSE_EXPIRED' bot/logs/testnet/api_ethusdt_stdout.log | tail -12
