#!/bin/bash
echo "=== candles processed after 13:00 (count per bot, top 10) ==="
for f in /opt/trading-bot/bot/logs/testnet/*_stdout.log; do
  n=$(grep -c "Candle closed" "$f" 2>/dev/null)
  last=$(grep "Candle closed" "$f" 2>/dev/null | tail -1 | cut -c1-19)
  echo "$(basename $f .log) total=$n last=[$last]"
done | sort -t= -k2 -rn | head -12
echo ""
echo "=== bots with stale candles (last Candle closed before 13:00) ==="
for f in /opt/trading-bot/bot/logs/testnet/*_stdout.log; do
  last=$(grep "Candle closed" "$f" 2>/dev/null | tail -1)
  ts=$(echo "$last" | cut -c1-16)
  if [[ "$ts" < "2026-11-07 13:00" ]]; then echo "$(basename $f .log): $ts"; fi
done | head -20
echo ""
echo "=== websocket queue overflow occurrences ==="
grep -l "QueueOverflow" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | wc -l
echo ""
echo "=== open symbols: SUI / FIL / OP / APT last 12 lines each ==="
for s in sui fil op apt; do
  echo "--- $s ---"
  tail -6 /opt/trading-bot/bot/logs/testnet/api_${s}usdt_stdout.log 2>/dev/null
done
