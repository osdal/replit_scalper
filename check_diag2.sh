#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet
echo "=== 1. MTLUSDT вход/sizing ==="
grep -hE "Size by|Market order placed|qty raised|\[CAP\]|Position verification" api_mtlusdt_stdout.log 2>/dev/null | tail -20
echo
echo "=== 2. ATOM / DOGE последние строки ==="
for s in atomusdt dogeusdt; do
  echo "--- $s"
  tail -8 api_${s}_stdout.log 2>/dev/null
done
echo
echo "=== 3. Кол-во EXPIRED-закрытий по символам ==="
grep -h "\[EXPIRED\] position in profit" api_*.log 2>/dev/null | sed -E 's/.*\[LIVE ([A-Z0-9]+)\].*/\1/' | sort | uniq -c
echo
echo "=== 4. Кол-во реальных входов (Market order placed) по дням ==="
grep -h "Market order placed" api_*.log 2>/dev/null | grep -oE "2026-1[01]-[0-9]{2}" | sort | uniq -c | tail -8
echo
echo "=== 5. Последние 10 Market order placed ==="
grep -h "Market order placed" api_*.log 2>/dev/null | tail -10
echo
echo "=== 6. Размер БД и число строк trades ==="
ls -lh /opt/trading-bot/data/bot.db
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('/opt/trading-bot/data/bot.db')
for q,label in [("SELECT COUNT(*) FROM trades","trades total"),
                ("SELECT COUNT(*) FROM trades WHERE status='rejected'","rejected"),
                ("SELECT COUNT(*) FROM trades WHERE status!='rejected'","accepted")]:
    print(label, con.execute(q).fetchone()[0])
PY
echo
echo "=== 7. Heartbeat свежесть (последние записи в логах) ==="
grep -h "Heartbeat" api_*.log 2>/dev/null | tail -3
