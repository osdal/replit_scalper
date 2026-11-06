#!/bin/bash
cd /opt/trading-bot
echo "=== ETH строка целиком (202828) ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
r = con.execute("SELECT * FROM trades WHERE id=202828").fetchone()
for k in r.keys():
    if r[k] is not None:
        print(f"  {k} = {r[k]}")
print()
print("=== все открытые: realized pnl в строке, leg_pnl, cycle_pnl, chain_depth ===")
for r in con.execute("""SELECT id,symbol,direction,qty,entry_price,pnl,leg_pnl,cycle_pnl,chain_depth,exit_reason,entry_time
FROM trades WHERE is_open=1 AND status!='rejected'"""):
    print(dict(r))
PY
echo
echo "=== логи ETH ==="
ls -la bot/logs/testnet/ | grep -i eth
echo "---"
tail -5 bot/logs/testnet/api_ethusdt_stdout.log 2>/dev/null
