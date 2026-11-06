#!/bin/bash
cd /opt/trading-bot
for s in eth sui near ada wld fil; do
  f=bot/logs/testnet/api_${s}usdt_stdout.log
  echo "=== $s ==="
  grep -hE 'reverse|REVERSE|EXPIRED|CUM_LOSS' "$f" 2>/dev/null | tail -4
done
