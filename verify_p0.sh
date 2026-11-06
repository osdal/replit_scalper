#!/bin/bash
sleep 60
echo "=== container ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Names}} | {{.Status}}'
echo
echo "=== bots ==="
docker exec replit_scalper-api-testnet bash -lc 'ps -eo args | grep "main.py" | grep -v grep | wc -l' 2>&1
echo
echo "=== gate ACTIVE line ==="
grep -h "Gate ACTIVE" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -3
echo
echo "=== new demand lines (with local weight) ==="
grep -h "RATE_LIMIT" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -8
echo
echo "=== gate file sample (fresh) ==="
cd /opt/trading-bot/bot/data
for f in $(ls -t binance_gate_testnet.json.* 2>/dev/null | head -3); do echo -n "$f: "; cat "$f"; echo; done
echo
echo "=== errors since restart ==="
docker logs replit_scalper-api-testnet --since 3m 2>&1 | grep -iE "error|traceback|exception" | grep -v "GateBlocked\|banned until" | tail -10
echo
echo "=== bans since restart ==="
grep -h "IP banned" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -5
echo
echo "=== open positions in db ==="
python3 - <<'PY'
import sqlite3
con=sqlite3.connect("file:/opt/trading-bot/data/bot.db?mode=ro", uri=True)
for r in con.execute("select id,symbol,direction,status,is_open from trades where is_open=1"):
    print(r)
PY
