#!/bin/bash
cd /opt/trading-bot
echo "=== DOGE после рестарта (sync/close) ==="
grep -hE "STARTUP|SYNC|reconcil|zombie|exchange shows no position|closing stale|re-final" bot/logs/testnet/api_dogeusdt_stdout.log 2>/dev/null | tail -20
echo
echo "=== APT: новый вход (sizing) ==="
grep -hE "Size by|Market order placed|qty raised|CAP\]" bot/logs/testnet/api_aptusdt_stdout.log 2>/dev/null | tail -10
echo
echo "=== DB: APT и DOGE текущее состояние ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT id,symbol,direction,ROUND(qty,4),ROUND(entry_price,6),ROUND(pnl,4),is_open,status,exit_reason,entry_time,exit_time FROM trades WHERE symbol IN ('APTUSDT','DOGEUSDT') ORDER BY id DESC LIMIT 8"):
    print(r)
PY
echo
echo "=== /trading/check для свободного символа (XRP) ==="
curl -s -X POST http://localhost:5000/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"XRPUSDT"}'
echo
echo "=== Свежие сигналы/входы за последние 20 мин ==="
grep -h "Market order placed" bot/logs/testnet/api_*.log 2>/dev/null | tail -5
