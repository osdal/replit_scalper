#!/bin/bash
echo "=== configs dirs ==="
ls /opt/trading-bot/configs/
echo
echo "=== testnet yaml: paper_balance ==="
grep -h "paper_balance" /opt/trading-bot/configs/testnet/config_atom.yaml 2>/dev/null || find /opt/trading-bot/configs -name "config_atom*.yaml" | head
echo
echo "=== live yaml: ключевые параметры ==="
for f in $(find /opt/trading-bot/configs -path "*live*" -name "config_atom.yaml" -o -path "*live*" -name "config_btc.yaml" | head -2); do
  echo "--- $f"
  grep -E "paper_balance|position_size_pct|max_position_notional|fixed_risk|leverage|reverse_" "$f"
done
echo
echo "=== fallback legacy sizing: где в коде ==="
grep -rn "fallback to legacy sizing" /opt/trading-bot/bot --include=*.py
echo
echo "=== gate shed события (все, кто shed>0) ==="
cd /opt/trading-bot/bot/logs/testnet
grep -h "shed=[1-9]" api_*_stdout.log | wc -l
grep -h "shed=[1-9]" api_*_stdout.log | sort | tail -5
echo
echo "=== gate blocked (всего) ==="
grep -h "gate blocked" api_*_stdout.log | wc -l
grep -h "gate blocked" api_*_stdout.log | cut -c1-13 | sort | uniq -c | tail -10
echo
echo "=== account balance endpoint ==="
curl -s http://localhost:5000/api/account 2>/dev/null | head -c 500
echo
curl -s http://localhost:5000/api/status 2>/dev/null | head -c 500
echo
echo "=== сколько виртуальных rejected trades в БД за сутки ==="
python3 - <<'PY'
import sqlite3
con=sqlite3.connect("file:/opt/trading-bot/data/bot.db?mode=ro", uri=True)
cur=con.cursor()
for q,label in [
 ("SELECT substr(entry_time,1,10), count(*) FROM trades WHERE status='rejected' GROUP BY 1 ORDER BY 1 DESC LIMIT 7","rejected/day"),
 ("SELECT count(*) FROM trades","total trades"),
 ("SELECT count(*) FROM trades WHERE status='rejected'","rejected total"),
 ("SELECT count(*) FROM trades WHERE is_open=1","open"),
]:
    print(label, list(cur.execute(q)))
PY
