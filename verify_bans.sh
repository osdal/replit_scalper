#!/bin/bash
L=/opt/trading-bot/bot/logs/testnet
echo "=== real ban events with timestamps since 06:30 ==="
grep -h -E "^2026-11-06 0[6-9]:[0-9]{2}:[0-9]{2}.*IP banned" $L/*.log 2>/dev/null | tail -5
echo
echo "=== count bans by hour (today) ==="
grep -h -oE "^2026-11-06 [0-9]{2}:[0-9]{2}:[0-9]{2}.*IP banned" $L/*.log 2>/dev/null | cut -c1-13 | sort | uniq -c | tail -8
echo
echo "=== last 5 ban lines with file/time ==="
grep -h -E "IP banned" $L/*.log 2>/dev/null | grep -E "^2026-11-06" | tail -5
echo
echo "=== after-deploy GateBlocked with timestamps ==="
grep -h -E "^2026-11-06 0[6-9]:[0-9]{2}.*GateBlocked" $L/*.log 2>/dev/null | tail -3
echo
echo "=== errors/tracebacks since restart ==="
docker logs replit_scalper-api-testnet --since 10m 2>&1 | grep -iE "traceback|exception|critical" | grep -viE "GateBlocked|banned" | tail -5
echo
echo "=== positions ==="
docker exec replit_scalper-api-testnet python3 - <<'PY'
import sqlite3
con=sqlite3.connect("file:/app/data/bot.db?mode=ro", uri=True)
for r in con.execute("select id,symbol,direction,status,is_open,entry_time from trades where is_open=1"):
    print(r)
PY
