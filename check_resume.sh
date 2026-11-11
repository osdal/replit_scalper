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
act = []
for f in glob.glob('bot/data/binance_gate_testnet.json*'):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    b = int(d.get('ban_until_ms', 0) or 0)
    if b > now + 5000:
        act.append((round((b-now)/1000), f))
print("active:", len(act))
for a in sorted(act)[-8:]:
    print(a)
PY
echo
echo "=== post-ban / startup events after 12:30 ==="
grep -ahE "post-ban|STARTUP] (staggered|Step 8)|\[PAUSE\]" bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep "2026-11-10 12:[3-9]" | tail -30
echo
echo "=== real (network) -1003 after 12:30 ==="
grep -ah "code=-1003" bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep "2026-11-10 12:[3-9]" | grep -v "gate blocked" | tail -10 || echo none
echo
echo "=== trades opened after 12:30 ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT id,symbol,direction,ROUND(entry_price,6),status,entry_time FROM trades WHERE entry_time >= '2026-11-10T12:30:00' ORDER BY id DESC LIMIT 15"):
    print(r)
print("open now:", list(con.execute("SELECT symbol,direction,entry_time FROM trades WHERE is_open=1")))
PY
echo
echo "=== status ==="
curl -s -m 10 http://localhost:5000/api/trading/status
echo
echo "=== warnings since 12:30 (count) ==="
grep -ah "2026-11-10 12:[3-9].*WARNING" bot/logs/testnet/api_*_stdout.log 2>/dev/null | wc -l
echo "=== errors since 12:30 (count) ==="
grep -ah "2026-11-10 12:[3-9].*ERROR" bot/logs/testnet/api_*_stdout.log 2>/dev/null | wc -l
