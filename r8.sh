#!/bin/bash
date -u
echo "=== candles per bot per day (5m; ожидается ~288) ==="
for s in ada fil inj near trx doge 1000pepe lsk; do
  f=/opt/trading-bot/bot/logs/testnet/api_${s}usdt_stdout.log
  printf "%-10s" "$s"
  for d in 08 09 10; do
    n=$(grep "Candle closed" "$f" 2>/dev/null | grep -c "2026-11-$d")
    printf "  11-%s=%4d" "$d" "$n"
  done
  echo
done
echo
echo "=== shed: распределение веса при блокировке ==="
grep -h "gate blocked: shed" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -oE "shed [0-9]+/[0-9]+" | awk -F'[ /]' '{print int($2/100)*100}' | sort -n | uniq -c | tail -10
echo
echo "=== ban windows: сколько длились (уникальные ban_until) ==="
grep -ohE "banned until [0-9]+" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | sort -u | tail -6
echo
echo "=== последние 60 минут: ошибки/предупреждения ==="
grep -hE " (ERROR|WARNING) " /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "^2026-11-10 (09|10):" | sed -E 's/^[0-9-]+ [0-9:]+ \[[A-Z]+ [A-Z0-9]+\] //' | sed -E 's/[0-9]+(\.[0-9]+)?/N/g' | cut -c1-70 | sort | uniq -c | sort -rn | head -8
