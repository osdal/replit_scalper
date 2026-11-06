#!/bin/bash
cd /opt/trading-bot
L=bot/logs/testnet
echo "=== positionRisk demand lines per hour today (all bots) ==="
grep -h "positionRisk" ${L}/api_*_stdout.log 2>/dev/null | grep "2026-11-06" | cut -c1-13 | sort | uniq -c | tail -12
echo
echo "=== count of RATE_LIMIT demand positionRisk last 15 min (all logs) ==="
grep -h "RATE_LIMIT] demand" ${L}/api_*_stdout.log 2>/dev/null | grep -c "positionRisk"
echo
echo "=== endpoint breakdown (last hour, approximate) ==="
grep -h "RATE_LIMIT] demand" ${L}/api_*_stdout.log 2>/dev/null | tail -3000 | sed -E 's/.*(GET|POST|DELETE) ([^ ]+).*/\2/' | sed -E 's#https://testnet.binancefuture.com##' | sort | uniq -c | sort -rn | head -12
echo
echo "=== positions table ==="
python3 - <<'PY'
import sqlite3
con=sqlite3.connect("data/bot.db")
print("open rows:", con.execute("SELECT COUNT(*) FROM trades WHERE is_open=1 AND status!='rejected'").fetchone()[0])
print("exposure notional (approx from bots.current_price*qty):")
for r in con.execute("SELECT symbol,direction,qty,entry_price FROM trades WHERE is_open=1 AND status!='rejected'"):
    print("  ", r)
PY
