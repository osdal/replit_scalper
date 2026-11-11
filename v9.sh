#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
python3 - <<'PY'
import datetime, glob, json, time
print("now epoch ms:", int(time.time()*1000))
print("now utc:", datetime.datetime.utcnow())
print("ban 1794313976816 ->", datetime.datetime.utcfromtimestamp(1794313976816/1000))
files = glob.glob('bot/logs/testnet/gate*') + glob.glob('data/gate*') + glob.glob('/tmp/gate*')
print("gate files found:", files[:10])
now = time.time()*1000
for f in files:
    try:
        d = json.load(open(f))
    except Exception:
        continue
    b = d.get('ban_until_ms', 0) or 0
    flag = "ACTIVE" if b > now else "expired"
    print(f, flag, b, (d.get('local_weight_1m'), d.get('used_weight_1m')))
PY
echo
echo "=== ps ==="
docker exec replit_scalper-api-testnet bash -lc 'ps aux | grep "[m]ain.py" | awk "{print \$2, \$12, \$13, \$14}"' 2>&1
