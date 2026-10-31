#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet || exit 1

echo "=== A. CRITICAL: примеры (31.10) ==="
grep -h "CRITICAL" api_*_stdout.log | grep "2026-10-31" | sed -E 's/^[0-9-]+ [0-9:]+ \[[A-Z]+ [A-Z0-9]+\] //' | cut -c1-110 | sort | uniq -c | sort -rn | head -15

echo
echo "=== B. -4164 по символам (все время) ==="
for f in api_*_stdout.log; do
  c=$(grep -c -- "-4164" "$f" 2>/dev/null)
  if [ "$c" -gt 0 ]; then echo "$c ${f#api_}"; fi
done | sort -rn | head -20

echo
echo "=== C. -4164 пример (полный текст) ==="
grep -h -- "-4164" api_*_stdout.log | sort | tail -3

echo
echo "=== D. paper_balance в конфигах (testnet) ==="
grep -h "paper_balance" /opt/trading-bot/configs/testnet/*.yaml | sort | uniq -c

echo
echo "=== E. live конфиги: min notional risk (notional cap) ==="
grep -hE "max_position_notional_usd|position_size_pct|paper_balance" /opt/trading-bot/configs/live/config_btc.yaml /opt/trading-bot/configs/live/config_eth.yaml 2>/dev/null

echo
echo "=== F. Баланс тестнета через API бота ==="
curl -s http://localhost:5000/api/bots/ATOMUSDT | head -c 1200
echo

echo
echo "=== G. Сколько уникальных сигналов в сутки (31.10) ==="
grep -h "New signal received" api_*_stdout.log | grep -c "2026-10-31"

echo
echo "=== H. 'gate blocked' за 31.10 по часам ==="
grep -h "gate blocked\|gate shed\|shed " api_*_stdout.log | grep "2026-10-31" | cut -c12-13 | sort | uniq -c

echo
echo "=== I. Сколько раз fallback legacy sizing (31.10) ==="
grep -h "fallback to legacy sizing" api_*_stdout.log | grep -c "2026-10-31"
grep -h "balance fetch failed" api_*_stdout.log | grep -c "2026-10-31"

echo
echo "=== J. Позиции: возраст и PnL (из API) ==="
curl -s http://localhost:5000/api/bots | python3 -c "
import sys,json,datetime
d=json.load(sys.stdin)
now=datetime.datetime.now(datetime.timezone.utc)
for b in d:
    p=b.get('position')
    if p:
        et=p.get('entry_time') or p.get('entryTime')
        print(b['symbol'], p.get('direction'), 'entry', p.get('entry_price'), 'qty', p.get('qty'), 'upnl', p.get('unrealized_pnl'), 'since', et)
"
