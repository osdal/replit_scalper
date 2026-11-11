#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== deploy ==="
git fetch origin main && git reset --hard FETCH_HEAD
echo
echo "=== yaml values ==="
grep -h "^max_positions:" bot/configs/testnet/config_doge.yaml bot/configs/testnet/config_lsk.yaml bot/configs/testnet/config_1000pepe.yaml bot/configs/testnet/config_fil.yaml
echo
echo "=== run init-db ==="
docker exec replit_scalper-api-testnet bash -lc 'pnpm run init-db 2>&1 | tail -3'
echo
echo "=== bots after init-db ==="
sudo python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT symbol,max_positions,max_positions_with_expired,is_running FROM bots WHERE is_running=1 ORDER BY symbol"):
    print(r)
PY
echo
echo "=== /check (all four) ==="
for s in DOGEUSDT LSKUSDT 1000PEPEUSDT FILUSDT; do
  printf "%s: " "$s"
  curl -s -m 10 -X POST http://localhost:5000/api/trading/check -H 'Content-Type: application/json' -d "{\"symbol\":\"$s\",\"direction\":\"LONG\"}" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print({k:d.get(k) for k in ('allowed','reason','max_positions','positions_open')})"
done
echo
echo "=== status ==="
curl -s -m 10 http://localhost:5000/api/trading/status | python3 -c "import json,sys; d=json.load(sys.stdin); print({k:v for k,v in d.items() if 'position' in k.lower() or 'paused' in k.lower()})"
echo
echo "=== bots alive ==="
docker exec replit_scalper-api-testnet bash -lc 'ps -eo pid,etime,cmd | grep "[m]ain.py"'
echo
echo "=== last [PAUSE] slots full lines ==="
grep -ah "\[PAUSE\] slots full" bot/logs/testnet/api_*_stdout.log 2>/dev/null | tail -5
