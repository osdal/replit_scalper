#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet
echo "=== -2022 за 03.11 ==="
grep -h "\-2022" api_*.log 2>/dev/null | grep -c "2026-11-03"
echo "=== -4164 за 03.11 ==="
grep -h "\-4164" api_*.log 2>/dev/null | grep -c "2026-11-03"
echo "=== -1003/-429 за 03.11 ==="
grep -hE "\-1003|\-429" api_*.log 2>/dev/null | grep -c "2026-11-03"
echo "=== rate-limit shed > 0 за 03.11 ==="
grep -h "RATE_LIMIT" api_*.log 2>/dev/null | grep "2026-11-03" | grep -cE "shed=[1-9]"
echo "=== fallback to legacy sizing за 03.11 ==="
grep -h "fallback to legacy sizing" api_*.log 2>/dev/null | grep -c "2026-11-03"
echo "=== последние отказы (по причинам) за 03.11 ==="
cd /opt/trading-bot
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT reject_reason, COUNT(*) FROM trades WHERE status='rejected' AND entry_time >= '2026-11-03' GROUP BY 1 ORDER BY 2 DESC"):
    print(r)
PY
