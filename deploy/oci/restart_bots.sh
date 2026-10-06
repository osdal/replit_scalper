#!/bin/bash
# Перезапуск ботов на Oracle, чтобы они подхватили новый конфиг.
#
#   ssh oracle-bot "/opt/trading-bot/deploy/oci/restart_bots.sh"            # testnet
#   ssh oracle-bot "/opt/trading-bot/deploy/oci/restart_bots.sh live"      # live
#
# Как это работает:
#   1. POST /api/bots/stop-all — SIGKILL процессов ботов этого стека.
#      Позиции на бирже и их биржевые SL/TP при этом НЕ трогаются.
#   2. Ждём, пока процессы действительно умрут.
#   3. POST /api/bots/<SYMBOL>/start для каждого config_*.yaml — бот стартует
#      заново и читает config_<symbol>.yaml, то есть подхватывает новое
#      значение position_size_pct.
#
# Контейнер api-server при этом НЕ перезапускается: иначе вместе с ним
# умрут и все боты. Перезапускаются только процессы ботов.
#
# Для live перед стартом делается Arm: в .env.live REQUIRE_ARM=true, без
# арминга бот не откроет позиции.
#
# ВАЖНО: live-ботов запускаем ТОЛЬКО по явному запросу оператора.
# Этот скрипт НЕ должен вызываться автоматически при деплое/рестарте стека.
set -uo pipefail

cd /opt/trading-bot

ENV_NAME="${1:-testnet}"
case "$ENV_NAME" in
  testnet) PORT=5000; CFG_DIR="bot/configs/testnet"; CONTAINER="replit_scalper-api-testnet" ;;
  live)    PORT=5001; CFG_DIR="bot/configs/live";    CONTAINER="replit_scalper-api-live" ;;
  *) echo "usage: $0 [testnet|live]" >&2; exit 2 ;;
esac

echo "=== Стек: $ENV_NAME (API :$PORT) ==="

echo "=== [1/4] Ждём готовности API ==="
for i in $(seq 1 60); do
  if curl -fsS -o /dev/null --max-time 3 "http://localhost:$PORT/api/bots"; then break; fi
  sleep 2
done
if ! curl -fsS -o /dev/null --max-time 3 "http://localhost:$PORT/api/bots"; then
  echo "API на :$PORT не отвечает. Запустить стек: sudo docker compose up -d" >&2
  exit 1
fi
echo "      API готов"

echo "=== [2/4] Останавливаю ботов ==="
curl -fsS -X POST "http://localhost:$PORT/api/bots/stop-all" || true
echo

echo "=== [3/4] Жду, пока процессы умрут ==="
for i in $(seq 1 30); do
  n=$(sudo docker exec "$CONTAINER" ps -eo args --no-headers 2>/dev/null | grep -c 'main.py')
  [ "$n" = "0" ] && break
  sleep 1
done
n=$(sudo docker exec "$CONTAINER" ps -eo args --no-headers 2>/dev/null | grep -c 'main.py')
echo "      осталось процессов: $n"

echo "=== [4/4] Запускаю ботов заново ==="
started=0
failed=""
for f in "$CFG_DIR"/config_*.yaml; do
  base=$(basename "$f" .yaml)
  sym=$(echo "${base#config_}" | tr '[:lower:]' '[:upper:]')USDT
  if [ "$ENV_NAME" = "live" ]; then
    curl -fsS -X POST --max-time 20 "http://localhost:$PORT/api/bots/$sym/arm" >/dev/null 2>&1 || true
  fi
  res=$(curl -fsS -X POST --max-time 90 "http://localhost:$PORT/api/bots/$sym/start" 2>&1) || res="failed: request error"
  case "$res" in
    *'"success":true'*) started=$((started+1)) ;;
    *) failed="$failed $sym" ;;
  esac
done

echo "      запущено: $started"
[ -n "$failed" ] && echo "      НЕ удалось:$failed"

echo
echo "=== Итог ==="
curl -fsS "http://localhost:$PORT/api/bots" \
  | python3 -c "import sys,json; d=json.load(sys.stdin); r=[b for b in d if b.get('is_running')]; print(f'ботов в БД: {len(d)}, запущено: {len(r)}')"
sudo docker exec "$CONTAINER" ps -eo args --no-headers 2>/dev/null | grep -c 'main.py' \
  | xargs -I{} echo "процессов в контейнере: {}"
free -m | head -2