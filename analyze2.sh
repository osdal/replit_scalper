#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet || exit 1

echo "=== A. Последние Size by (с датой) ==="
grep -h "Size by position_size_pct" api_*_stdout.log | tail -8

echo
echo "=== B. Реальные ban-1003 строки за 31.10 ==="
grep -h "^-1003\|code=-1003\|-1003)" api_*_stdout.log | grep "2026-10-31" | tail -15

echo
echo "=== C. Что матчится как 418 (примеры) ==="
grep -h "418" api_*_stdout.log | grep "2026-10-31" | head -10

echo
echo "=== D. WS ошибки (примеры, 31.10) ==="
grep -hiE "\[WS\].*(error|disconnect|closed|reconnect)" api_*_stdout.log | grep "2026-10-31" | tail -15

echo
echo "=== E. -2022 ReduceOnly (примеры, 31.10) ==="
grep -h "2022" api_*_stdout.log | grep "2026-10-31" | tail -8

echo
echo "=== F. -2011 Unknown order (примеры, 31.10) ==="
grep -h "2011" api_*_stdout.log | grep "2026-10-31" | tail -8

echo
echo "=== G. RATE_LIMIT gate: последние строки ==="
grep -h "RATE_LIMIT" api_*_stdout.log | tail -5

echo
echo "=== H. shed>0 (сколько запросов отброшено гейтом) за 31.10 ==="
grep -h "RATE_LIMIT" api_*_stdout.log | grep "2026-10-31" | grep -oE "shed=[0-9]+" | sort | uniq -c | sort -rn | head

echo
echo "=== I. -1003 за последние 6 часов по ботам ==="
grep -h "code=-1003" api_*_stdout.log | grep -E "2026-10-31 (0[7-9]|1[0-3]):" | sed -E 's/.*api_([a-z0-9]+)_stdout.*/\1/' | head -3
grep -l "code=-1003" api_*_stdout.log | xargs grep -h "code=-1003" 2>/dev/null | grep -E "2026-10-31 (0[7-9]|1[0-3]):" | wc -l
