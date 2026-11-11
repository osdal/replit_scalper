#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== processes ==="
docker exec replit_scalper-api-testnet bash -lc 'ps -eo pid,etime,cmd | grep "[m]ain.py"' 2>&1
echo
echo "=== active bans in gate files ==="
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
        print(f, "ACTIVE", round((b-now)/1000), "s left", "used", d.get('used_weight_1m'), "local", d.get('local_weight_1m'))
PY
echo
echo "=== post-ban / startup events last 30 min ==="
grep -ahE "post-ban|STARTUP] (staggered|Step 8)|\[PAUSE\]" bot/logs/testnet/api_*_stdout.log 2>/dev/null | tail -30
echo
echo "=== real (network) -1003 last 30 min ==="
grep -ah "code=-1003" bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep "2026-11-10 12:[4-5]" | grep -v "gate blocked" | tail -10 || echo none
echo
echo "=== trades opened after 12:30 ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT id,symbol,direction,ROUND(entry_price,6),status,entry_time,ROUND(pnl,4),exit_reason,cycle_close_reason FROM trades WHERE entry_time >= '2026-11-10T12:30:00' AND status!='rejected' ORDER BY id DESC LIMIT 20"):
    print(r)
print("open now:", list(con.execute("SELECT symbol,direction,entry_time FROM trades WHERE is_open=1")))
PY
echo
echo "=== status ==="
curl -s -m 10 http://localhost:5000/api/trading/status
echo
echo "=== [PAUSE] slots full counts ==="
grep -ah "\[PAUSE\] slots full" bot/logs/testnet/api_*_stdout.log 2>/dev/null | wc -l