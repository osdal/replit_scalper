#!/bin/bash
date -u
echo "=== 'skipping cycle' per hour (Nov 8-10) ==="
grep -h "skipping cycle" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null \
  | grep -E "^2026-11-(08|09|10)" | awk '{print $1" "$2}' | cut -c1-13 | uniq -c | tail -30
echo
echo "=== total skipping-cycle msgs per day ==="
grep -h "skipping cycle" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | awk '{print $1}' | sort | uniq -c | tail -6
echo
echo "=== distinct ban_until per day (ms -> UTC) ==="
python3 - << 'EOF'
import re, glob, datetime, collections
pat = re.compile(r'^(2026-11-(?:08|09|10)) .*banned until (\d+)')
seen = collections.defaultdict(set)
for f in glob.glob('/opt/trading-bot/bot/logs/testnet/*_stdout.log'):
    try:
        for line in open(f, errors='ignore'):
            m = pat.match(line)
            if m:
                seen[m.group(1)].add(int(m.group(2)))
    except Exception:
        pass
for d in sorted(seen):
    vals = sorted(seen[d])
    print(d, len(vals), [datetime.datetime.utcfromtimestamp(v/1000).strftime('%H:%M') for v in vals][:8])
EOF
