#!/bin/bash
date -u
echo "=== NEW ban detections (no block) by minute-of-5m ==="
grep -h "no block)" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null \
 | grep -E "^2026-11-(08|09|10)" \
 | awk '{print $2}' | cut -c4-5 | sort -n | uniq -c
echo
echo "=== NEW ban detections: exact times (Nov 9-10) ==="
grep -h "no block)" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null \
 | grep -E "^2026-11-(09|10)" | awk '{print $2}' | sort | uniq -c | head -40
echo
echo "=== sample demand line (full) ==="
grep -h "demand " /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | tail -2 | cut -c1-400
echo
echo "=== demand lines: requests per minute (Nov 10) ==="
grep -h "demand " /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -oE "req=[0-9]+" | cut -d= -f2 | sort -n | uniq -c | tail -10
echo
echo "=== hour-of-day ban starts (Nov 8-10) ==="
grep -h "no block)" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null \
 | grep -E "^2026-11-(08|09|10)" | cut -c12-13 | sort | uniq -c
