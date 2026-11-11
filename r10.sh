#!/bin/bash
date -u
echo "=== shed lines sample (with time) ==="
grep -h "gate blocked: shed" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "^2026-11-(08|09|10)" | tail -6 | cut -c1-130
echo
echo "=== shed per day ==="
grep -h "gate blocked: shed" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | awk '{print $1}' | sort | uniq -c | tail -8
echo
echo "=== per-bot shed count Nov 10 ==="
grep -h "gate blocked: shed" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "^2026-11-10" | grep -oE "\[LIVE [A-Z0-9]+\]" | sort | uniq -c | sort -rn
echo
echo "=== local weight max per fresh file (live procs only) ==="
python3 - << 'EOF'
import glob, json, time
now = time.time()
for f in glob.glob('/opt/trading-bot/bot/data/binance_gate_testnet.json.*'):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    if now - d.get('ts', 0) <= 86400:
        print(f.rsplit('.',1)[-1], 'age=%.0fs' % (now-d.get('ts',0)), 'local=', d.get('local_weight_1m'), 'used=', d.get('used_weight_1m'))
EOF
echo
echo "=== BINANCE_TESTNET inside bot process env ==="
docker exec replit_scalper-api-testnet bash -lc "tr '\\0' '\\n' < /proc/137/environ | grep -E 'BINANCE_TESTNET|BINANCE_WEIGHT_SOURCE'"
