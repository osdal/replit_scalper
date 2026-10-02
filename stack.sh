#!/usr/bin/env bash
# ============================================================
#  Запуск обоих стеков в Docker на Linux (Oracle Cloud / любой VPS).
#  Аналог start_docker.ps1 для Windows.
#
#  Что делает:
#    1. docker compose up -d  -> testnet (5000/5173) + live (5001/5176)
#    2. Ждёт готовности обоих API
#    3. Arm + Start для live-ботов
#
#  Testnet-боты поднимаются сами (AUTO_RESTART_BOTS по умолчанию true).
#  Live — нет: в .env.live AUTO_RESTART_BOTS=false (намеренно, реальные деньги).
#
#  Использование:
#    ./stack.sh                                  # live-боты: 1000PEPEUSDT
#    ./stack.sh BTCUSDT ETHUSDT                  # свой набор live-ботов
#    NO_LIVE_BOTS=1 ./stack.sh                   # только стеки
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

LIVE_SYMBOLS="${LIVE_SYMBOLS:-1000PEPEUSDT}"

# docker compose (v2) или docker-compose (v1)
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "[FAIL] docker compose not found" >&2
  exit 1
fi

wait_api() {
  local url="$1" seconds="$2"
  for ((i = 0; i < seconds; i++)); do
    if curl -fsS -o /dev/null --max-time 3 "$url"; then return 0; fi
    sleep 2
  done
  return 1
}

echo "============================================"
echo "  Starting Docker stacks (testnet + live)"
echo "============================================"

echo "[1/4] docker compose up -d ..."
$DC up -d || { echo "[FAIL] compose up failed" >&2; exit 1; }

echo "[2/4] Waiting for API servers..."
if wait_api "http://localhost:5000/api/bots" 90; then
  echo "      testnet API OK  - http://localhost:5000"
else
  echo "      WARNING: testnet API (:5000) not ready - $DC logs api-testnet"
fi
if wait_api "http://localhost:5001/api/bots" 90; then
  echo "      live API OK     - http://localhost:5001"
else
  echo "      WARNING: live API (:5001) not ready - $DC logs api-live"
fi

echo "[3/4] Starting live bots..."
if [ "${NO_LIVE_BOTS:-0}" = "1" ] || [ -z "$LIVE_SYMBOLS" ]; then
  echo "      skipped"
else
  for sym in $LIVE_SYMBOLS; do
    u="$(echo "$sym" | tr '[:lower:]' '[:upper:]')"
    arm=$(curl -fsS -X POST --max-time 15 "http://localhost:5001/api/bots/$u/arm" 2>&1 || echo "arm failed: $arm")
    start=$(curl -fsS -X POST --max-time 60 "http://localhost:5001/api/bots/$u/start" 2>&1 || echo "start failed: $start")
    echo "      $u: $arm | $start"
  done
fi

echo "[4/4] Status"
$DC ps --format "      {{.Name}}\t{{.Status}}" 2>/dev/null || $DC ps

echo ""
echo "============================================"
echo "  TESTNET dashboard: http://localhost:5173"
echo "  LIVE dashboard:    http://localhost:5176"
echo "============================================"
echo "Bot count:"
echo "  testnet: $(docker exec replit_scalper-api-testnet ps -eo args --no-headers 2>/dev/null | grep -c 'main.py')"
echo "  live:    $(docker exec replit_scalper-api-live ps -eo args --no-headers 2>/dev/null | grep -c 'main.py')"
