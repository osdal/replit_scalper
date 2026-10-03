#!/bin/bash
# Чистый старт testnet-стеков на Oracle: ротация логов, свежая БД, все боты.
#
# САМО по себе НЕ закрывает позиции на бирже и НЕ удаляет старую БД — это
# отдельные шаги, см. ниже. Скрипт только поднимает стек и запускает ботов.
#
# Полный сброс перед сбором статистики (из PowerShell):
#   # 1. остановить ботов на других машинах, иначе они удвоят позиции
#   curl.exe -X POST http://localhost:5000/api/bots/stop-all
#   # 2. закрыть всё на бирже (из контейнера testnet на Oracle)
#   docker cp scripts/reset_exchange.py oracle-bot:/tmp/
#   ssh oracle-bot "sudo docker cp /tmp/reset_exchange.py \
#       replit_scalper-api-testnet:/tmp/ && sudo docker exec -w /app/bot \
#       replit_scalper-api-testnet python3 /tmp/reset_exchange.py --confirm"
#   # 3. удалить БД и state-файлы на Oracle
#   ssh oracle-bot "cd /opt/trading-bot && sudo rm -f data/bot.db data/bot.db-wal \
#       data/bot.db-shm bot/state_testnet_*.json bot/bot.lock.testnet.*"
#   # 4. запустить стек и всех ботов
#   ssh oracle-bot "/opt/trading-bot/deploy/oci/start_testnet_fresh.sh"
#
# Запуск: ssh oracle-bot "/opt/trading-bot/deploy/oci/start_testnet_fresh.sh"
set -uo pipefail

cd /opt/trading-bot

echo "=== [1/4] Ротация старых логов ==="
if [ -d bot/logs/testnet ]; then
  mv bot/logs/testnet "bot/logs/testnet_old_$(date +%Y%m%d_%H%M%S)"
  echo "      старые логи -> bot/logs/testnet_old_*"
fi

echo "=== [2/4] Поднимаю api-testnet (init-db создаст свежую БД) ==="
sudo docker compose up -d api-testnet 2>&1 | tail -2

echo "=== [3/4] Жду API на :5000 ==="
for i in $(seq 1 60); do
  if curl -fsS -o /dev/null --max-time 3 "http://localhost:5000/api/bots"; then
    echo "      API готов"
    break
  fi
  sleep 2
done

echo "=== [4/4] Запускаю ботов из bot/configs/testnet/ ==="
started=0
for f in bot/configs/testnet/config_*.yaml; do
  base=$(basename "$f" .yaml)          # config_1000pepe
  sym=$(echo "${base#config_}" | tr '[:lower:]' '[:upper:]')USDT
  res=$(curl -fsS -X POST --max-time 60 "http://localhost:5000/api/bots/$sym/start" 2>&1) || res="failed"
  case "$res" in
    *'"success":true'*) started=$((started+1)) ;;
    *) echo "      ! $sym: $res" ;;
  esac
done
echo "      запущено: $started"

echo
echo "=== Итог ==="
curl -fsS "http://localhost:5000/api/bots" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); r=[b for b in d if b.get('is_running')]; print(f'ботов в БД: {len(d)}, запущено: {len(r)}')"
free -m | head -2