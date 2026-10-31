#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet
echo "=== A. Traceback после 13:01 (точный фильтр) ==="
grep -hE "^2026-10-31 13:(0[1-9]|[1-5][0-9])" api_*_stdout.log | grep -c "Traceback"
grep -hA3 -E "^2026-10-31 13:(0[1-9]|[1-5][0-9]).*Traceback" api_*_stdout.log | head -20
echo
echo "=== B. Сигналов на бота за сегодня (топ-10) ==="
for f in api_*_stdout.log; do
  n=$(grep -c "2026-10-31.*New signal received" "$f")
  echo "$n ${f#api_}"
done | sort -rn | head -10
echo
echo "=== C. Сколько реальных входов за сегодня (open+closed) ==="
python3 - <<'PY'
import sqlite3
c=sqlite3.connect("file:/opt/trading-bot/data/bot.db?mode=ro", uri=True).cursor()
print("entries today:", list(c.execute("SELECT count(*) FROM trades WHERE entry_time>='2026-10-31' AND status!='rejected'"))[0][0])
print("signals today:", list(c.execute("SELECT count(*) FROM trades WHERE entry_time>='2026-10-31' AND status='rejected'"))[0][0])
PY
echo
echo "=== D. Выходы/причины за сегодня ==="
python3 - <<'PY'
import sqlite3
c=sqlite3.connect("file:/opt/trading-bot/data/bot.db?mode=ro", uri=True).cursor()
for r in c.execute("SELECT exit_reason,count(*),round(sum(pnl),3) FROM trades WHERE entry_time>='2026-10-31' AND status!='rejected' GROUP BY 1"): print(r)
PY
echo
echo "=== E. Балансы из логов (последние 5 упоминаний) ==="
grep -h "free_margin=" api_*_stdout.log | sort | tail -3
grep -h "Balance" api_atomusdt_stdout.log | sort | tail -3
