#!/bin/bash
echo "=== deaf bots (last candle before 13:00 UTC) ==="
deaf=0; alive=0
for f in /opt/trading-bot/bot/logs/testnet/*_stdout.log; do
  last=$(grep "Candle closed" "$f" 2>/dev/null | tail -1 | cut -c1-16)
  if [[ -z "$last" || "$last" < "2026-11-07 13:00" ]]; then deaf=$((deaf+1)); else alive=$((alive+1)); fi
done
echo "deaf=$deaf alive=$alive"
echo ""
echo "=== queue overflow timestamps (first/last) ==="
grep -h -B1 "QueueOverflow" /opt/trading-bot/bot/logs/testnet/api_btcusdt_stdout.log 2>/dev/null | tail -3
echo ""
echo "=== exchange stop-loss placed for open symbols ==="
for s in sui fil op apt; do
  echo "--- $s ---"
  grep -h "Stop-loss placed\|STOP_MARKET\|stop placed\|backstop" /opt/trading-bot/bot/logs/testnet/api_${s}usdt_stdout.log 2>/dev/null | tail -2
done
echo ""
echo "=== stale/deaf bots: are they stuck inside ban wait? (last line each) ==="
for f in /opt/trading-bot/bot/logs/testnet/*_stdout.log; do
  last=$(grep "Candle closed" "$f" 2>/dev/null | tail -1 | cut -c1-16)
  if [[ -z "$last" || "$last" < "2026-11-07 13:00" ]]; then
    echo "$(basename $f .log | sed 's/api_//'): [$(tail -1 "$f" | cut -c1-110)]"
  fi
done | head -25
