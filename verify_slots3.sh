#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== git HEAD ==="
git log --oneline -3
echo
echo "=== init-db env fallback present in container src? ==="
docker exec replit_scalper-api-testnet bash -lc 'grep -n "ENV_MAX_POSITIONS" /app/artifacts/api-server/src/init-db.ts'
echo
echo "=== trades since 18:40 ==="
sudo python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
rows = list(con.execute("SELECT id,symbol,direction,status,entry_time,exit_time,ROUND(pnl,4),exit_reason FROM trades WHERE entry_time >= '2026-11-11T18:40:00' ORDER BY id"))
print(rows if rows else "none")
print("open:", list(con.execute("SELECT symbol,direction,entry_time,ROUND(unrealized_pnl,4) FROM trades WHERE is_open=1")))
PY
echo
echo "=== errors/PAUSE since 18:50 ==="
grep -ahE "\[PAUSE\] slots full|ERROR" bot/logs/testnet/api_*_stdout.log 2>/dev/null | grep -aE "2026-11-11 (18:5|19:)" | tail -10 || echo none
echo
echo "=== ban state ==="
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
        print(f, "ban", round((b-now)/1000), "s left ->", time.strftime('%H:%M UTC', time.gmtime(b/1000)))
PY
