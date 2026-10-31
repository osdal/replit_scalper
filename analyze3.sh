#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet || exit 1

echo "=== A. Size by за 30-31.10 (по времени) ==="
grep -h "Size by" api_*_stdout.log | sort | tail -15

echo
echo "=== B. Строки 'banned -1003' по дням ==="
grep -h "banned -1003" api_*_stdout.log | cut -c1-10 | sort | uniq -c | tail -10

echo
echo "=== C. 'banned -1003' за 31.10 (по времени) ==="
grep -h "banned -1003" api_*_stdout.log | grep "2026-10-31" | sort | tail -10

echo
echo "=== D. Все варианты -1003 за 31.10 ==="
grep -h -- "-1003" api_*_stdout.log | grep "2026-10-31" | sed -E 's/^[0-9-]+ [0-9:]+ \[[A-Z]+ [A-Z0-9]+\] //' | cut -c1-100 | sort | uniq -c | sort -rn | head

echo
echo "=== E. 418/-418/418 I'm a teapot за 31.10 ==="
grep -hE "code=-418|-418\)|teapot|IP banned|banned until" api_*_stdout.log | grep "2026-10-31" | sort | tail -10

echo
echo "=== F. Gate shed / banned / reject статистика по дням (30-31.10) ==="
grep -h "RATE_LIMIT" api_*_stdout.log | grep -E "2026-10-3[01]" | grep -oE "shed=[0-9]+" | sort | uniq -c

echo
echo "=== G. equity / free margin (последние) ==="
grep -hiE "free_margin|equity|balance" api_*_stdout.log | grep -E "2026-10-3[01]" | sort | tail -10
