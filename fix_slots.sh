#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== before ==="
sudo python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT symbol,max_positions,max_positions_with_expired,is_running FROM bots WHERE is_running=1 ORDER BY symbol"):
    print(r)
PY
echo
echo "=== set max_positions=3 for running testnet bots ==="
sudo python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
cur = con.execute("UPDATE bots SET max_positions=3 WHERE is_running=1")
con.commit()
print("rows updated:", cur.rowcount)
for r in con.execute("SELECT symbol,max_positions,is_running FROM bots WHERE is_running=1 ORDER BY symbol"):
    print(r)
PY
echo
echo "=== /trading/status max_positions fields ==="
curl -s -m 10 http://localhost:5000/api/trading/status | python3 -c "import json,sys; d=json.load(sys.stdin); print({k:v for k,v in d.items() if 'position' in k.lower() or 'slot' in k.lower() or 'paused' in k.lower()})"
echo
echo "=== /check for DOGEUSDT (slot view) ==="
curl -s -m 10 -X POST http://localhost:5000/api/trading/check -H 'Content-Type: application/json' -d '{"symbol":"DOGEUSDT","direction":"LONG"}' | python3 -c "import json,sys; d=json.load(sys.stdin); print({k:d.get(k) for k in ('allowed','reason','max_positions','max_positions_with_expired','open_positions','occupied','totalOccupied')})"
