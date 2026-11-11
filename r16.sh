#!/bin/bash
date -u
echo "=== demand req values by day (median-ish) ==="
for d in 08 09 10; do
  echo -n "Nov $d: "
  grep -h "demand " /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null \
    | grep -E "^2026-11-$d" | grep -oE "req=[0-9]+" | cut -d= -f2 \
    | awk '{a[NR]=$1} END {if(NR==0){print "none";exit} asort(a); print "n="NR, "median="a[int(NR/2)+1], "p90="a[int(NR*0.9)+1], "max="a[NR]}'
done
echo
echo "=== sample demand lines from a quiet period (Nov 8 18:00-18:10) ==="
grep -h "demand " /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "^2026-11-08 18:0" | head -6 | cut -c1-260
echo
echo "=== how many candles processed vs on_candle errors ==="
grep -h "on_candle error" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | awk '{print $1}' | sort | uniq -c | tail -5
echo
echo "=== requests per candle estimate: count 'positionRisk' demand entries ==="
grep -h "demand " /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "^2026-11-09" | grep -oE "GET [^ ]+ req=[0-9]+" | sed -E 's#https://[^ ]+##' | head -10
