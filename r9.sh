#!/bin/bash
date -u
echo "=== gate files now ==="
python3 - << 'EOF'
import glob, json, time, datetime
now = time.time()
for f in sorted(glob.glob('/opt/trading-bot/bot/data/binance_gate_testnet.json.*')):
    try:
        d = json.load(open(f))
    except Exception:
        continue
    age = now - d.get('ts', 0)
    ban = d.get('ban_until_ms', 0)
    left = (ban - now*1000)/1000 if ban else 0
    print(f"{f.rsplit('.',1)[-1]:>6} age={age:6.0f}s used={d.get('used_weight_1m',0):5} local={d.get('local_weight_1m',0):5} ban_left={left:8.0f}s")
EOF
echo
echo "=== lines with -1003 (last 8) ==="
grep -h "\-1003" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "^2026-11-10" | tail -8 | cut -c1-150
echo
echo "=== ban events today (recorded) ==="
grep -hE "banned until" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "^2026-11-10" | grep -vE "gate blocked|same ban|skipping" | tail -6 | cut -c1-150
echo
echo "=== distinct ban_until first/last seen ==="
grep -ohE "banned until [0-9]+" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | sort | uniq -c | tail -8
