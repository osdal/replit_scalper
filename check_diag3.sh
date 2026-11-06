#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet
echo "=== ATOM: EXPIRED / CUM / cum-loss / reverse ==="
grep -hE "\[EXPIRED\]|CUM_LOSS|cum-loss|REVERSE_|TP_CLOSE|residual" api_atomusdt_stdout.log 2>/dev/null | tail -30
echo
echo "=== DOGE: то же ==="
grep -hE "\[EXPIRED\]|CUM_LOSS|cum-loss|REVERSE_|TP_CLOSE|residual" api_dogeusdt_stdout.log 2>/dev/null | tail -20
echo
echo "=== MTL: то же ==="
grep -hE "\[EXPIRED\]|CUM_LOSS|cum-loss|REVERSE_|TP_CLOSE|residual|Heartbeat" api_mtlusdt_stdout.log 2>/dev/null | tail -20
echo
echo "=== bots: armed/running поля ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('/opt/trading-bot/data/bot.db')
con.row_factory = sqlite3.Row
cols = [r[1] for r in con.execute("PRAGMA table_info(bots)").fetchall()]
print("cols:", [c for c in cols if 'arm' in c or 'run' in c or 'enab' in c or 'state' in c])
for r in con.execute("SELECT symbol, * FROM bots WHERE symbol IN ('ATOMUSDT','DOGEUSDT','MTLUSDT')"):
    d = dict(r)
    print({k: d[k] for k in d if ('arm' in k or 'run' in k or 'enab' in k or 'state' in k or k=='symbol')})
PY
