#!/bin/bash
cd /opt/trading-bot
L=bot/logs/testnet
echo "=== ZEC tail ==="
tail -25 ${L}/api_zecusdt_stdout.log 2>&1
echo
echo "=== log size by symbol (top 8) ==="
ls -S ${L}/api_*_stdout.log 2>/dev/null | head -8 | xargs -I{} sh -c 'echo "$(du -h {} | cut -f1) {}"'
echo
echo "=== recent RATE_LIMIT demand samples ==="
grep -h "\[RATE_LIMIT\] demand" ${L}/api_*.log 2>/dev/null | tail -12
echo
echo "=== gate stats lines ==="
grep -h "\[RATE_LIMIT\] Gate" ${L}/api_*.log 2>/dev/null | tail -5
echo
echo "=== shed lines count by hour today ==="
grep -h "gate shed" ${L}/api_*.log 2>/dev/null | grep "2026-11-06" | cut -c1-13 | sort | uniq -c
