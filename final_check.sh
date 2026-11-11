#!/bin/bash
date -u
echo "=== live container ==="
docker ps --filter name=replit_scalper-api-live --format '{{.Names}} {{.Status}}'
echo "=== live bot processes ==="
docker exec replit_scalper-api-live bash -lc "ps aux | grep main.py | grep -v grep | wc -l" 2>&1
echo "=== live env ==="
docker exec replit_scalper-api-live bash -lc 'env' 2>&1 | grep -E 'BINANCE_TESTNET|DAILY_LOSS_LIMIT|MAX_POSITIONS|LIVE_DAILY' || true
echo "=== testnet candles (last closed per kept bot) ==="
for s in ada fil inj near trx doge 1000pepe lsk; do
  f=/opt/trading-bot/bot/logs/testnet/api_${s}usdt_stdout.log
  printf "%-10s " "$s"
  grep 'Candle closed' "$f" 2>/dev/null | tail -1 | cut -c1-64
done
echo "=== active bans (fresh <120s) ==="
python3 - << 'EOF'
import glob, json, time
now = time.time()
act = []
fresh = 0
for f in glob.glob("/opt/trading-bot/bot/data/binance_gate_testnet.json.*"):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    if now - d.get("ts", 0) <= 120:
        fresh += 1
        if d.get("ban_until_ms", 0) > now * 1000:
            act.append((f.rsplit(".", 1)[-1], round((d["ban_until_ms"] - now * 1000) / 60000, 1)))
print("fresh gate files:", fresh, "active bans:", len(act), act[:5])
EOF
