#!/bin/bash
cd /opt/trading-bot
echo "=== схема trades ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("PRAGMA table_info(trades)"):
    print(r[1], r[2])
PY
echo
echo "=== -1003 по минутам (05.11) ==="
grep -h '\-1003' bot/logs/testnet/api_*.log | grep '2026-11-05' | sed -E 's/^([0-9-]+ [0-9]{2}:[0-9]{2}).*/\1/' | sort | uniq -c | sort -rn | head -12
echo
echo "=== -1003 всего по дням ==="
for d in 2026-11-02 2026-11-03 2026-11-04 2026-11-05; do echo "$d: $(grep -h '\-1003' bot/logs/testnet/api_*.log | grep -c $d)"; done
echo
echo "=== вызвавший запрос (пример строки) ==="
grep -h '\-1003' bot/logs/testnet/api_*.log | grep '2026-11-05' | tail -3
