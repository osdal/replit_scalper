#!/bin/bash
sleep 90
echo "=== bots ==="
docker exec replit_scalper-api-testnet bash -lc 'ps -eo args' 2>&1 | grep -c "main.py"
echo "=== Gate ACTIVE today ==="
grep -h "^2026-11-06 0[7-9]" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | grep "Gate ACTIVE" | tail -3
echo "=== bans since 06:50 ==="
grep -h -E "^2026-11-06 (06:5[0-9]|0[7-9]:[0-9]{2}):[0-9]{2}.*IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | wc -l
echo "=== shed lines today ==="
grep -h -E "^2026-11-06 0[7-9]" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | grep "RATE_LIMIT] demand" | grep -c "shed=0"
echo "=== errors ==="
docker logs replit_scalper-api-testnet --since 3m 2>&1 | grep -iE "traceback|exception|critical" | grep -viE "GateBlocked|banned" | tail -5
echo "=== open trades ==="
python3 -c "
import sqlite3
con=sqlite3.connect('file:/opt/trading-bot/data/bot.db?mode=ro', uri=True)
print(list(con.execute('select id,symbol,direction from trades where is_open=1')))
"
