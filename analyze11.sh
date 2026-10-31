#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet
echo "=== A. Процессы ботов ==="
ps aux | grep -c "[m]ain.py"
echo
echo "=== B. Ошибки после рестарта (13:01+) ==="
grep -hE "ERROR|WARNING|Traceback" api_*_stdout.log | awk '$1" "$2 >= "2026-10-31 13:01"' | sed -E 's/^[0-9-]+ [0-9:]+ \[[A-Z]+ [A-Z0-9]+\] //' | cut -c1-100 | sort | uniq -c | sort -rn | head -20
echo
echo "=== C. Heartbeat/arm статус: сколько running ==="
curl -s http://localhost:5000/api/bots | python3 -c "
import sys,json
d=json.load(sys.stdin)
print('total',len(d),'running',sum(1 for b in d if b.get('is_running')),'armed',sum(1 for b in d if b.get('armed')))
import datetime
now=datetime.datetime.now(datetime.timezone.utc)
stale=[b['symbol'] for b in d if b.get('last_heartbeat') and (now-datetime.datetime.fromisoformat(b['last_heartbeat'].replace('Z','+00:00'))).total_seconds()>180]
print('stale heartbeats:',len(stale), stale[:5])
"
echo
echo "=== D. Входы после 13:00 ==="
grep -h "New signal received\|position opened\|OPENED\|Order placed\|ORDER" api_*_stdout.log | awk '$1" "$2 >= "2026-10-31 13:00"' | tail -20
echo
echo "=== E. Сколько сделок сегодня по статусам ==="
python3 - <<'PY'
import sqlite3
c=sqlite3.connect("file:/opt/trading-bot/data/bot.db?mode=ro", uri=True).cursor()
for r in c.execute("SELECT status, count(*) FROM trades WHERE entry_time>='2026-10-31' GROUP BY 1"): print(r)
PY
