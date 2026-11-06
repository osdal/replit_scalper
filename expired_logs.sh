#!/bin/bash
cd /opt/trading-bot
echo "=== [EXPIRED] за 05.11 ==="
grep -h '\[EXPIRED\]' bot/logs/testnet/api_*.log | grep '2026-11-05' | tail -10
echo
echo "=== NEAR: последние EXPIRED/закрытие ==="
grep -hE '\[EXPIRED\]|EXPIRED_PROFIT|EXPIRED_CLOSE' bot/logs/testnet/api_nearusdt_stdout.log | tail -8
echo
echo "=== открытые сейчас (БД) ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,symbol,direction,ROUND(qty,4) qty,entry_time
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time"""):
    print(dict(r))
PY
