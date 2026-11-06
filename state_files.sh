#!/bin/bash
cd /opt/trading-bot
echo "=== state файлы ==="
find bot -maxdepth 3 -name "*DOGE*" -o -maxdepth 3 -name "*state*" | head -20
echo
echo "=== DOGE state ==="
for f in $(find bot -maxdepth 3 -iname "*doge*state*" -o -maxdepth 3 -iname "state_doge*"); do echo "--- $f"; cat "$f"; echo; done
echo
echo "=== где хранится state (grep) ==="
grep -rn "state_.*json\|STATE_FILE\|_state_path" bot/position_tracker.py | head -10
echo
echo "=== DOGE: TP/SL в БД vs на бирже ==="
echo "БД: sl=0.074443 tp1=0.074974 (значения LONG-ноги)"
echo "Биржа: TP limit BUY 0.073150, backstop BUY 0.07729"
echo
echo "=== MTL последние закрытия/TP-попытки ==="
grep -hE "EXPIRED|TP_CLOSE|TP placed|Position closed|EXTERNAL_CLOSE" bot/logs/testnet/api_mtlusdt_stdout.log 2>/dev/null | tail -12
