#!/bin/bash
cd /opt/trading-bot || exit 1
date -u
echo "=== bot processes ==="
docker exec replit_scalper-api-testnet bash -lc 'ps aux | grep "[m]ain.py" | wc -l' 2>&1
echo
echo "=== startup lines per bot (last) ==="
for f in bot/logs/testnet/*.log; do
  echo "--- $f"
  grep -aE '\[STARTUP\] (staggered|Step 7|Step 8)|\[PAUSE\]|Traceback|ERROR' "$f" | tail -4
done 2>/dev/null | head -80
echo
echo "=== ban-related last 24h ==="
grep -ahoE 'banned until [0-9]+|post-ban resume in [0-9]+s|post-ban cooldown' bot/logs/testnet/*.log 2>/dev/null | tail -20 || echo none
echo
echo "=== gate ban state ==="
python3 - <<'PY'
import glob,json,time
now=time.time()
for f in glob.glob('bot/logs/testnet/gate*.json')+glob.glob('data/gate*.json'):
    try:
        d=json.load(open(f))
    except Exception as e:
        continue
    b=d.get('ban_until_ms',0)
    if b and b>now*1000:
        print(f, 'BAN until', b, 'left_s', round((b/1000-now)))
print('done')
PY
