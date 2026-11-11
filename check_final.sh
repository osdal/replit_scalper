#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== processes ==="
docker exec replit_scalper-api-testnet bash -lc 'ps -eo pid,etime,cmd | grep "[m]ain.py"' 2>&1
echo
echo "=== is_running in DB ==="
sudo python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT symbol,max_positions,is_running FROM bots WHERE is_running=1 ORDER BY symbol"):
    print(r)
PY
echo
echo "=== active bans ==="
python3 - <<'PY'
import glob, json, time
now = time.time()*1000
act = []
for f in sorted(glob.glob('bot/data/binance_gate_testnet.json*')):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    b = int(d.get('ban_until_ms', 0) or 0)
    if b > now + 5000:
        act.append((f, round((b-now)/1000), d.get('used_weight_1m'), d.get('local_weight_1m')))
print("active bans:", len(act))
for a in act: print(a)
PY
echo
echo "=== real network bans last 24h ==="
grep -ah "code=-1003" bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep -v "gate blocked" | grep -E "2026-11-1[01]" | tail -8 || echo none
echo
echo "=== trades since 2026-11-10 16:00 ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("""SELECT id,symbol,direction,ROUND(pnl,4),ROUND(cycle_pnl,4),chain_depth,exit_reason,cycle_close_reason,entry_time,exit_time,status
FROM trades WHERE entry_time >= '2026-11-10T16:00:00' ORDER BY id DESC LIMIT 30"""):
    print(r)
print("open:", list(con.execute("SELECT symbol,direction,entry_time FROM trades WHERE is_open=1")))
print("--- PnL since 16:00 by reason ---")
for r in con.execute("""SELECT exit_reason, COUNT(*), ROUND(SUM(pnl),4) FROM trades
WHERE entry_time >= '2026-11-10T16:00:00' AND status!='rejected' AND exit_time IS NOT NULL
GROUP BY exit_reason ORDER BY 3"""):
    print(r)
print("--- total ---")
for r in con.execute("""SELECT COUNT(*), ROUND(SUM(pnl),4), ROUND(SUM(commission),4) FROM trades
WHERE entry_time >= '2026-11-10T16:00:00' AND status!='rejected' AND exit_time IS NOT NULL"""):
    print(r)
PY
echo
echo "=== status API ==="
curl -s -m 10 http://localhost:5000/api/trading/status
echo
echo "=== [PAUSE] slots full (since Nov10 16:00) ==="
grep -ah "\[PAUSE\] slots full" bot/logs/testnet/api_*_stdout.log 2>/dev/null | tail -5
grep -ah "\[PAUSE\] slots full" bot/logs/testnet/api_*_stdout.log 2>/dev/null | wc -l
echo
echo "=== post-ban resume events ==="
grep -ah "post-ban resume" bot/logs/testnet/api_*_stdout.log 2>/dev/null | tail -8
echo
echo "=== errors last 6h ==="
grep -ah "ERROR" bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep -E "2026-11-11 (1[2-8])" | tail -10 || echo none