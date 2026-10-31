#!/bin/bash
# Анализ логов testnet: баны, лимиты, reverse-цепочки, ошибки API
cd /opt/trading-bot/bot/logs/testnet || exit 1

echo "=== 1. Признаки банов/лимитов Binance (за всё время) ==="
for pat in "IP ban" "banned" "418" "429" "Too many" "rate limit" "weight" "EAPI" "Unauthorized" "Invalid API"; do
  n=$(grep -ci "$pat" api_*_stdout.log 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
  echo "$pat: $n"
done

echo
echo "=== 2. Примеры ошибок Binance (последние 20 уникальных) ==="
grep -hoiE "(code=-?[0-9]+|HTTP [45][0-9][0-9]|banned|Too many|rate limit)[^\"]{0,120}" api_*_stdout.log 2>/dev/null \
  | sort | uniq -c | sort -rn | head -25

echo
echo "=== 3. Ошибки / исключения ==="
for pat in "Traceback" "ERROR" "CRITICAL" "Exception" "order_rejected" "insufficient" "Precision" "LOT_SIZE" "MIN_NOTIONAL"; do
  n=$(grep -ci "$pat" api_*_stdout.log 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
  echo "$pat: $n"
done

echo
echo "=== 4. Reverse-цепочка ==="
for pat in "REVERSE" "chain_failed" "chain limit" "force-close" "force_close" "REVERSE_CHAIN_STOP"; do
  n=$(grep -ci "$pat" api_*_stdout.log 2>/dev/null | awk -F: '{s+=$2} END {print s+0}')
  echo "$pat: $n"
done

echo
echo "=== 5. Причины отклонения входов ==="
grep -hoE "reason[\"':= ]+(max_positions|loss_streak|daily_loss_limit|check_error|cooldown|no_signal)[a-z_]*" api_*_stdout.log 2>/dev/null \
  | sort | uniq -c | sort -rn | head -20
grep -hoiE "reject_reason[\"':= ]+[a-z_]+" api_*_stdout.log 2>/dev/null | sort | uniq -c | sort -rn | head -20

echo
echo "=== 6. Сайзинг (последние записи Size by) ==="
grep -h "Size by" api_*_stdout.log 2>/dev/null | tail -10

echo
echo "=== 7. Открытия/закрытия позиций за сутки ==="
echo "OPEN:  $(grep -hci "position opened\|OPENED\|Открыта позиция" api_*_stdout.log 2>/dev/null | awk '{s+=$1} END {print s+0}')"
echo "CLOSE: $(grep -hci "position closed\|CLOSED\|Закрыта позиция" api_*_stdout.log 2>/dev/null | awk '{s+=$1} END {print s+0}')"

echo
echo "=== 8. Watchdog / самостопы ==="
grep -hciE "watchdog|self-stop|stop_reason" api_*_stdout.log 2>/dev/null | awk '{s+=$1} END {print "watchdog/stops: " s+0}'

echo
echo "=== 9. Последние 40 строк одного активного бота (atomi) ==="
tail -40 api_atomusdt_stdout.log 2>/dev/null
