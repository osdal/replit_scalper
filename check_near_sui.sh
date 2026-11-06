#!/bin/bash
cd /opt/trading-bot
for s in nearusdt suiusdt; do
  echo "=== $s: последние 30 (без rate-limit) ==="
  grep -hE "2026-11-04 21:" bot/logs/testnet/api_${s}_stdout.log | grep -vE "RATE_LIMIT|PRICE\]|Heartbeat|Candle closed" | tail -30
  echo
done
echo "=== NEAR state ==="
cat bot/state_testnet_nearusdt.json 2>/dev/null | head -32
echo
echo "=== SUI state (хвост) ==="
python3 -c "import json;d=json.load(open('bot/state_testnet_suiusdt.json'));print({k:d.get(k) for k in ('direction','entry_price','sl_price','tp1_price','total_qty','remaining_qty','backstop_algo_id','exchange_tp_price','is_reverse')})" 2>/dev/null
