#!/bin/bash
# Безопасный деплой api-server. Выполняется НА ХОСТЕ ORACLE (не на Windows),
# чтобы не зависеть от того, как именно локально настроен ssh.
#
#   ЛОКАЛЬНО (один раз):
#     scp <файл.ts> oracle-bot:/opt/trading-bot/_incoming/
#   ЗАТЕМ:
#     ssh oracle-bot "bash /opt/trading-bot/tools/deploy_api.sh <путь/относительно/репо>"
#
#   Пример:
#     scp artifacts/api-server/src/routes/trades.ts oracle-bot:/opt/trading-bot/_incoming/
#     ssh oracle-bot "bash /opt/trading-bot/tools/deploy_api.sh artifacts/api-server/src/routes/trades.ts"
#
# Делает в безопасном порядке:
#   1. бэкап ТЕКУЩИХ файлов в _backup_api_<stamp>/
#   2. baseline СМОУК-ТЕСТОМ со СТАРОЙ версии (если старая сломана — стоп)
#   3. копирование из _incoming/ в рабочее место
#   4. рестарт api-live / api-testnet + ожидание /healthz
#   5. смоук-тест НОВОЙ версии против baseline
#   6. при FAIL — откат из бэкапа, рестарт, повторный смоук
#
# ВАЖНО: бэкап берётся ДО копирования, иначе в бэкап попадёт новый файл и
# откат восстановит сломанную версию.
#
# Смоук-тест ТОЛЬКО читает: GET /bots /trades /trades/stats /recovery/config
# /live/daily-loss /live/drawdown /signals/stats /healthz
# и POST /trading/check с reserve=false. Никаких DELETE /trades,
# /live/close-all, /bots/stop-all — тест обязан быть безопасен на продакшене.

set -u

ROOT="${ROOT:-/opt/trading-bot}"
INCOMING="${INCOMING:-$ROOT/_incoming}"
SMOKE="$ROOT/tools/smoke_api.py"
LIVE="${LIVE:-http://localhost:5001}"
TESTNET="${TESTNET:-http://localhost:5000}"
STAMP=$(date +%Y%m%d_%H%M%S)
BACKUP="$ROOT/_backup_api_$STAMP"

die() { echo "!! $*"; exit 1; }

[ $# -ge 1 ] || die "укажи путь файла относительно корня репо, напр. artifacts/api-server/src/routes/trades.ts"
[ -f "$SMOKE" ] || die "нет $SMOKE"

for f in "$@"; do
  [ -f "$INCOMING/$f" ] || die "нет $INCOMING/$f — сначала scp файла в $INCOMING/"
done

echo "=== 1/6 бэкап СТАРЫХ файлов (до копирования!) ==="
mkdir -p "$BACKUP" || die "не создать $BACKUP"
for f in "$@"; do
  rel="${f#artifacts/api-server/}"
  if [ -f "$ROOT/$f" ]; then
    mkdir -p "$BACKUP/$(dirname "$rel")"
    cp "$ROOT/$f" "$BACKUP/$rel" || die "бэкап $f не удался"
    echo "  backup: $f"
  else
    echo "  (на Oracle нет $f — это НОВЫЙ файл, откатывать будет нечего)"
  fi
done

echo "=== 2/6 baseline СТАРОЙ версии ==="
if [ "${FORCE:-0}" = "1" ]; then
  echo "  FORCE=1 — предварительный смоук пропущен (старая версия заведомо сломана)"
  BL=""
else
  python3 "$SMOKE" --base "$LIVE" --save "/tmp/baseline_live_$STAMP.json" >/tmp/smoke_pre.log 2>&1
  PRE_RC=$?
  tail -3 /tmp/smoke_pre.log | sed 's/^/  /'
  if [ $PRE_RC -ne 0 ]; then
    echo "!! СТАРАЯ версия уже не проходит смоук — деплой отменён."
    echo "   Файлы НЕ тронуты. Разберись с текущим состоянием сначала."
    echo "   Если деплой и есть исправление — запусти с FORCE=1."
    exit 1
  fi
  BL="/tmp/baseline_live_$STAMP.json"
fi

echo "=== 3/6 копирование из $INCOMING/ ==="
for f in "$@"; do
  mkdir -p "$(dirname "$ROOT/$f")"
  cp "$INCOMING/$f" "$ROOT/$f" || die "копирование $f не удалось"
  echo "  $f -> md5 $(md5sum "$ROOT/$f" | cut -d' ' -f1)"
done

echo "=== 4/6 рестарт api ==="
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "  DRY_RUN=1 — рестарт пропущен, новый код НЕ активирован"
  echo "=== DRY-RUN завершён: файлы на месте, но старый процесс продолжает работать ==="
  exit 0
fi
cd "$ROOT" || die "нет $ROOT"
docker compose restart api-live >/dev/null 2>&1 || die "рестарт api-live упал"
docker compose restart api-testnet >/dev/null 2>&1 || echo "  (api-testnet не рестартован)"

code=""
for i in $(seq 1 30); do
  sleep 3
  code=$(curl -s -o /dev/null -w '%{http_code}' "$LIVE/api/healthz" 2>/dev/null)
  [ "$code" = "200" ] && { echo "  api-live поднялся за ${i}x3s"; break; }
done

# API мог не подняться вовсе (синтаксис, битый импорт). Без откатa это
# оставило бы продакшен сломанным — поэтому этот случай тоже ведём в откат.
if [ "$code" != "200" ]; then
  echo "!! api-live НЕ поднялся после деплоя (healthz=$code) — откат"
  POST_RC=1
else
  echo "=== 5/6 смоук-тест НОВОЙ версии ==="
  if [ -n "$BL" ] && [ -f "$BL" ]; then
    python3 "$SMOKE" --base "$LIVE" --baseline "$BL" > /tmp/smoke_post.log 2>&1
  else
    python3 "$SMOKE" --base "$LIVE" > /tmp/smoke_post.log 2>&1
  fi
  POST_RC=$?
  tail -22 /tmp/smoke_post.log | sed 's/^/  /'
fi

if [ $POST_RC -eq 0 ]; then
  echo "=== 6/6 PASS — деплой принят ==="
  # Ждём, пока testnet реально поднимется: без этого его смоук может попасть
  # в момент, когда API уже рестартнуло, но ещё не отвечает — и вернуть
  # ложный FAIL, хотя ничего не сломано. Готовность проверяем по /bots
  # (тяжелее healthz: требует инициализации БД), плюс ретрай самого смоука.
  TCODE=""
  for i in $(seq 1 40); do
    sleep 3
    TCODE=$(curl -s -o /dev/null -w '%{http_code}' "$TESTNET/api/bots" 2>/dev/null)
    [ "$TCODE" = "200" ] && { echo "  api-testnet готов за ${i}x3s"; break; }
  done
  if [ "$TCODE" = "200" ]; then
    for attempt in 1 2; do
      python3 "$SMOKE" --base "$TESTNET" --quiet 2>/dev/null | grep -E "ИТОГ|RESULT" | sed "s/^/  testnet(попытка $attempt): /"
      TRC=${PIPESTATUS[0]}
      [ "$TRC" = "0" ] && break
      sleep 10
    done
    [ "$TRC" != "0" ] && echo "  testnet: НЕ проходит смоук после ретраев — проверь вручную"
  else
    echo "  testnet: api-testnet не ответил на /bots (код=$TCODE) — проверь вручную"
  fi
  # Уберём использованный incoming, чтобы следующем деплой не подхватил старое
  for f in "$@"; do rm -f "$INCOMING/$f"; done
  exit 0
fi

echo "=== 6/6 FAIL — ОТКАТ ==="
for f in "$@"; do
  rel="${f#artifacts/api-server/}"
  if [ -f "$BACKUP/$rel" ]; then
    cp "$BACKUP/$rel" "$ROOT/$f"
    echo "  откачен: $f"
  else
    rm -f "$ROOT/$f"
    echo "  удалён новый файл: $f"
  fi
done
docker compose restart api-live >/dev/null 2>&1
docker compose restart api-testnet >/dev/null 2>&1
code=""
for i in $(seq 1 30); do
  sleep 3
  code=$(curl -s -o /dev/null -w '%{http_code}' "$LIVE/api/healthz" 2>/dev/null)
  [ "$code" = "200" ] && break
done
echo "  повторный смоук после откатa:"
python3 "$SMOKE" --base "$LIVE" --quiet 2>/dev/null | sed 's/^/  /'
RC=$?
if [ $RC -eq 0 ]; then
  echo "  ОТКАТ УСПЕШЕН — API в рабочем состоянии"
else
  echo "!! ОТКАТ НЕ ПОМОГ — дело может быть не в деплое, смотри /tmp/smoke_post.log"
fi
exit 1
