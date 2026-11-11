#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== startup completion ==="
grep -ah "Step 8: loaded.*candles for warm-up\|starting candle polling" bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep "2026-11-10 12:" | tail -15
echo
echo "=== recent signals (after restart) ==="
grep -ah "New signal received" bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep "2026-11-10 12:" | tail -10
echo
echo "=== recent trades (after restart) ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT id,symbol,direction,ROUND(entry_price,6),status,entry_time,ROUND(pnl,4),exit_reason FROM trades WHERE entry_time >= '2026-11-10T12:40:00' AND status!='rejected' ORDER BY id"):
    print(r)
print("open:", list(con.execute("SELECT symbol,direction,entry_time FROM trades WHERE is_open=1")))
PY
echo
echo "=== any errors since restart ==="
grep -ah "ERROR" bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep "2026-11-10 12:" | tail -10 || echo "no errors"
echo
echo "=== bans check ==="
python3 - <<'PY'
import glob, json, time
now = time.time()*1000
for f in sorted(glob.glob('bot/data/binance_gate_testnet.json*')):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    b = int(d.get('ban_until_ms', 0) or 0)
    if b > now + 5000:
        print(f, "ACTIVE", round((b-now)/1000), "s left")
PY