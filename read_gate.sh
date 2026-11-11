#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
for f in bot/data/binance_gate_testnet.json.* ; do
  echo "--- $f"
  cat "$f"
  echo
done
echo "=== gate code: fields ==="
grep -n "local_weight_1m\|used_weight_1m\|weight_1m\|ban_until_ms" bot/binance_gate.py | head -40
