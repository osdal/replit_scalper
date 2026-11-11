#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== gate files anywhere ==="
find /opt/trading-bot -maxdepth 4 -name "binance_gate.json*" -printf "%p  %TY-%Tm-%Td %TH:%TM  %s\n" 2>/dev/null | head -20
echo
echo "=== content of newest gate files ==="
for f in $(find /opt/trading-bot -maxdepth 4 -name "binance_gate.json*" 2>/dev/null | head -6); do echo "--- $f"; cat "$f"; echo; done
echo
echo "=== grid engine env on testnet ==="
docker exec replit_scalper-api-testnet sh -lc 'env | grep -E "GRID_ENGINE|GRID_AUTO|BINANCE_" | sort'
echo
echo "=== grid-engine.json (if any) ==="
cat data/grid-engine.json 2>/dev/null || echo "(none)"
echo
echo "=== grids with engine=server (active) ==="
python3 - <<'PY'
import sqlite3, os
for db in ("/opt/trading-bot/data/bot.db",):
    if not os.path.exists(db): continue
    c = sqlite3.connect(db)
    try:
        rows = list(c.execute("SELECT uid,symbol,status,engine FROM grids WHERE engine='server' LIMIT 20"))
        print(db, "->", rows)
        print("counts:", list(c.execute("SELECT status,engine,COUNT(*) FROM grids GROUP BY status,engine")))
    except Exception as e:
        print(db, "err", e)
PY
