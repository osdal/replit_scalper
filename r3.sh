#!/bin/bash
date -u
echo "=== bot process start times (etimes) ==="
docker exec replit_scalper-api-testnet bash -lc "ps -eo pid,etimes,args | grep 'main.py' | grep -v grep | awk '{print \$1, int(\$2/3600)\"h\", \$NF}'" 2>&1 | head -12
echo
echo "=== real bans since 2026-11-07 14:00 ==="
grep -h "Way too many requests" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "^2026-11-(07 1[4-9]:|07 2[0-3]:|08|09|10)" | wc -l
echo "--- last 3 ---"
grep -h "Way too many requests" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | tail -3 | cut -c1-120
echo
echo "=== queue overflow total ==="
grep -h "QueueOverflow" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | wc -l
echo
echo "=== gate blocks (per day) ==="
grep -h "gate blocked" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | awk '{print $1}' | sort | uniq -c | tail -6
echo
echo "=== ERROR/WARNING counts by day (since Nov 7 14:00) ==="
grep -hE " (ERROR|WARNING) " /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | awk '{print $1, $3}' | sort | uniq -c | sort -k2,2 -k3,3 | tail -20
