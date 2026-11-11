#!/bin/bash
date -u
echo "=== real ban lines on Nov 8-10 ==="
grep -h "Way too many requests" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "^2026-11-(08|09|10)" | cut -c1-130
echo
echo "=== gate block reasons (Nov 8-10) ==="
grep -h "gate blocked" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -oE "gate blocked: [a-z ]+" | sort | uniq -c | sort -rn | head -10
echo
echo "=== top WARNING types Nov 8-10 (normalized) ==="
grep -h " WARNING " /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null \
  | grep -E "^2026-11-(08|09|10)" \
  | sed -E 's/^[0-9-]+ [0-9:]+ \[[A-Z]+ [A-Z0-9]+\] WARNING //' \
  | sed -E 's/[0-9]+(\.[0-9]+)?/N/g' | cut -c1-90 | sort | uniq -c | sort -rn | head -15
echo
echo "=== top ERROR types Nov 8-10 (normalized) ==="
grep -h " ERROR " /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null \
  | grep -E "^2026-11-(08|09|10)" \
  | sed -E 's/^[0-9-]+ [0-9:]+ \[[A-Z]+ [A-Z0-9]+\] ERROR //' \
  | sed -E 's/[0-9]+(\.[0-9]+)?/N/g' | cut -c1-90 | sort | uniq -c | sort -rn | head -15
echo
echo "=== last candle per kept bot ==="
for s in ada fil inj near trx doge 1000pepe lsk; do
  f=/opt/trading-bot/bot/logs/testnet/api_${s}usdt_stdout.log
  printf "%-10s " "$s"
  grep 'Candle closed' "$f" 2>/dev/null | tail -1 | cut -c1-46
done
