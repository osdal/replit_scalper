#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet
echo "=== A. Sizing: последние 15 строк Size by position_size_pct ==="
grep -h "Size by position_size_pct" api_*.log 2>/dev/null | tail -15
echo
echo "=== B. free_margin значения (уникальные) с 31.10 14:00 ==="
grep -h "free_margin=\$" api_*.log 2>/dev/null | grep -E "2026-(10-31 1[4-9]|10-31 2[0-3]|11-0[1-3])" | sed -E 's/.*free_margin=(\$[0-9.]+).*/\1/' | sort | uniq -c | sort -rn | head -10
echo
echo "=== C. fallback to legacy sizing (после деплоя) ==="
grep -h "fallback to legacy sizing" api_*.log 2>/dev/null | grep -E "2026-(10-31 1[4-9]|10-31 2[0-3]|11-0[1-3])" | wc -l
echo
echo "=== D. -2022 за 31.10 после 14:00, 01-03.11 ==="
grep -h "(-2022)\|-2022" api_*.log 2>/dev/null | grep -E "2026-(10-31 1[4-9]|10-31 2[0-3]|11-0[1-3])" | wc -l
echo "--- по дням:"
grep -h "\-2022" api_*.log 2>/dev/null | grep -oE "2026-1[01]-[0-9]{2}" | sort | uniq -c | tail -10
echo
echo "=== E. -1003 / -429 после деплоя ==="
grep -h "(-1003)\|(-429)" api_*.log 2>/dev/null | grep -E "2026-(10-31 1[4-9]|10-31 2[0-3]|11-0[1-3])" | wc -l
echo
echo "=== F. EXPIRED закрытия ==="
grep -h "\[EXPIRED\]" api_*.log 2>/dev/null | grep -E "2026-(10-31 1[4-9]|10-31 2[0-3]|11-0[1-3])" | tail -20
echo
echo "=== G. EXPIRED_CLOSE в логах ==="
grep -h "EXPIRED_CLOSE" api_*.log 2>/dev/null | tail -10
echo
echo "=== H. Ошибки за последние 2 дня (уникальные шаблоны) ==="
grep -h "ERROR" api_*.log 2>/dev/null | grep -E "2026-(11-0[1-3])" | sed -E 's/.*ERROR //' | sed -E 's/[0-9]+/N/g' | sort | uniq -c | sort -rn | head -15
