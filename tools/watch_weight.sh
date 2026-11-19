#!/bin/bash
# watch_weight.sh — фоновый наблюдатель за нагрузкой на Binance и числом live-ботов.
# ТОЛЬКО ЧИТАЕТ: ничего не перезапускает, не останавливает и не трогает ботов.
#
#   watch_weight.sh run                 основной цикл (запускает systemd)
#   watch_weight.sh report [YYYY-MM-DD] сводка по часам за день (по умолчанию сегодня, UTC)
#   watch_weight.sh test-alert          отправить тестовое сообщение в Telegram
#   watch_weight.sh install             установить и запустить как службу systemd (нужен sudo)
#   watch_weight.sh uninstall           остановить и удалить службу (данные в logs/monitor остаются)
#
# Что делает run:
#   * каждые SAMPLE_SEC секунд (10) спрашивает у Binance счётчик веса IP (запрос ping, weight 1)
#     и пишет в $OUT/weight_<дата>.csv строки «время,вес»;
#   * раз в минуту считает процессы ботов в контейнере live и пишет в $OUT/bots_<дата>.csv;
#   * шлёт тревогу в Telegram (не чаще раза в COOLDOWN_SEC на каждый тип):
#       - вес >= WARN_WEIGHT (1500 из 2400);
#       - Binance ответил 418/429 (возможен бан IP);
#       - число ботов меньше EXPECT_BOTS (отключается файлом $OUT/mute_bots);
#       - Binance не отвечает 6 раз подряд;
#   * раз в сутки в SUMMARY_HOUR_UTC шлёт сводку за вчера.
# Токен и chat_id берутся из /opt/trading-bot/.env.live (или .env), в логи не пишутся.
set -u

APP="${APP_DIR:-/opt/trading-bot}"
OUT="${MON_DIR:-$APP/logs/monitor}"
SAMPLE_SEC="${SAMPLE_SEC:-10}"
WARN_WEIGHT="${WARN_WEIGHT:-1500}"
EXPECT_BOTS="${EXPECT_BOTS:-40}"
CONTAINER="${CONTAINER:-replit_scalper-api-live}"
COOLDOWN_SEC="${COOLDOWN_SEC:-900}"
PING_URL="${PING_URL:-https://fapi.binance.com/fapi/v1/ping}"
SUMMARY_HOUR_UTC="${SUMMARY_HOUR_UTC:-06}"
MAX_LOOPS="${MAX_LOOPS:-0}"   # 0 = без ограничения (ненулевое значение нужно только для тестов)

mkdir -p "$OUT"

read_env_var() {
  local key="$1" f v
  for f in "$APP/.env.live" "$APP/.env"; do
    [ -f "$f" ] || continue
    v=$(grep -E "^${key}=" "$f" | tail -1 | cut -d= -f2- | tr -d "\"'\r")
    if [ -n "$v" ]; then printf '%s' "$v"; return; fi
  done
}

send_tg() {
  local token chat
  token=$(read_env_var TELEGRAM_BOT_TOKEN); chat=$(read_env_var TELEGRAM_CHAT_ID)
  [ -n "$token" ] && [ -n "$chat" ] || return 0
  curl -s -m 10 -X POST "https://api.telegram.org/bot${token}/sendMessage" \
    --data-urlencode "chat_id=${chat}" --data-urlencode "text=$1" >/dev/null 2>&1 || true
}

# notify <тип> <текст>: пишет в alerts.log всегда, в Telegram — не чаще раза в COOLDOWN_SEC на тип
notify() {
  local key="$1" msg="$2" now last
  now=$(date +%s); last=$(cat "$OUT/.last_$key" 2>/dev/null || echo 0)
  [ $((now - last)) -ge "$COOLDOWN_SEC" ] || return 0
  echo "$now" > "$OUT/.last_$key"
  echo "$(date -u +%FT%TZ) ALERT[$key] $msg" >> "$OUT/alerts.log"
  send_tg "$msg"
}

docker_cmd() {
  if docker ps >/dev/null 2>&1; then docker "$@"; else sudo docker "$@"; fi
}

sample() {
  local hdr
  hdr=$(curl -s -m 8 -D - -o /dev/null "$PING_URL" 2>/dev/null | tr -d '\r')
  CODE=$(printf '%s\n' "$hdr" | awk 'NR==1{print $2}')
  WEIGHT=$(printf '%s\n' "$hdr" | awk -F': *' 'tolower($1)=="x-mbx-used-weight-1m"{print $2; exit}')
  case "$WEIGHT" in ''|*[!0-9]*) WEIGHT="" ;; esac
}

count_bots() {
  local out n ts day
  out=$(docker_cmd exec "$CONTAINER" ps -eo args 2>/dev/null) || return 0   # контейнер недоступен: не шумим
  n=$(printf '%s\n' "$out" | grep -c 'main.py')
  ts=$(date -u +%FT%TZ); day=$(date -u +%F)
  echo "$ts,$n" >> "$OUT/bots_$day.csv"
  if [ "$n" -lt "$EXPECT_BOTS" ] && [ ! -f "$OUT/mute_bots" ]; then
    notify bots "⚠️ Работает ботов live: $n из $EXPECT_BOTS (время $(date -u +%H:%M) UTC). Чтобы заглушить это предупреждение: touch $OUT/mute_bots"
  fi
}

report_day() {
  local day="${1:-$(date -u +%F)}" f
  f="$OUT/weight_$day.csv"
  [ -f "$f" ] || { echo "нет данных за $day"; return 0; }
  awk -F, '
    $2 ~ /^[0-9]+$/ {
      h=substr($1,12,2); n[h]++; s[h]+=$2
      if (!(h in m) || $2>m[h]) { m[h]=$2; t[h]=substr($1,12,8) }
      if ($2>=1000) hi[h]++
      if ($2>gm) { gm=$2; gt=substr($1,12,8) }
      tot++; sum+=$2; if ($2>=1000) ghi++
    }
    $2=="NA" { na++ }
    END {
      printf "Вес запросов Binance (счётчик за минуту, лимит 2400), UTC\n"
      for (i=0;i<24;i++) { h=sprintf("%02d",i)
        if (h in n) printf "  %s:00  замеров=%-4d среднее=%-5d максимум=%-5d (в %s)  >=1000: %d\n", h, n[h], s[h]/n[h], m[h], t[h], hi[h]+0 }
      if (tot) printf "ИТОГО: замеров=%d среднее=%d максимум=%d (в %s) замеров>=1000: %d, не ответил: %d\n", tot, sum/tot, gm, gt, ghi+0, na+0
    }' "$f"
}

daily_summary() {
  local y; y=$(date -u -d yesterday +%F)
  local txt bots
  txt=$(report_day "$y" | tail -1)
  bots=$(tail -1 "$OUT/bots_$(date -u +%F).csv" 2>/dev/null | cut -d, -f2)
  send_tg "📊 Сводка за $y. ${txt:-нет данных}. Ботов сейчас: ${bots:-?}."
}

install_service() {
  sudo mkdir -p /opt/trading-bot/tools || return 1
  sudo install -m 755 "$0" /opt/trading-bot/tools/watch_weight.sh || return 1
  sudo tee /etc/systemd/system/binance-watch.service >/dev/null <<'UNIT'
[Unit]
Description=Binance weight and live bots watchdog (read-only)
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
ExecStart=/opt/trading-bot/tools/watch_weight.sh run
Restart=always
RestartSec=15
Nice=10

[Install]
WantedBy=multi-user.target
UNIT
  sudo systemctl daemon-reload
  sudo systemctl enable --now binance-watch.service
  sleep 3
  echo "статус службы: $(systemctl is-active binance-watch.service)"
}

case "${1:-run}" in
  install) install_service; exit $? ;;
  uninstall) sudo systemctl disable --now binance-watch.service; sudo rm -f /etc/systemd/system/binance-watch.service; sudo systemctl daemon-reload; echo "удалено"; exit 0 ;;
  report) report_day "${2:-}"; exit 0 ;;
  test-alert) send_tg "✅ Тест: наблюдатель за Binance работает ($(date -u +%FT%TZ))"; echo "отправлено (если токен и chat_id найдены)"; exit 0 ;;
  run) ;;
  *) echo "usage: $0 run|report [дата]|test-alert|install|uninstall" >&2; exit 2 ;;
esac

per_min=$((60 / SAMPLE_SEC)); [ "$per_min" -ge 1 ] || per_min=1
loops=0; nofetch=0
while :; do
  sample
  ts=$(date -u +%FT%TZ); day=$(date -u +%F)
  if [ -n "$WEIGHT" ]; then
    echo "$ts,$WEIGHT" >> "$OUT/weight_$day.csv"; nofetch=0
    if [ "$WEIGHT" -ge "$WARN_WEIGHT" ]; then
      notify weight "⚠️ Вес запросов Binance $WEIGHT из 2400 (порог $WARN_WEIGHT), $(date -u +%H:%M:%S) UTC"
    fi
  else
    echo "$ts,NA,$CODE" >> "$OUT/weight_$day.csv"; nofetch=$((nofetch + 1))
  fi
  case "$CODE" in
    418|429) notify ban "🛑 Binance ответил HTTP $CODE на ping: возможен бан IP. $(date -u +%H:%M:%S) UTC. Проверьте: grep 'banned until' в логах" ;;
  esac
  if [ "$nofetch" -ge 6 ]; then
    notify nofetch "⚠️ Binance не отвечает на ping ${nofetch} раз подряд (сеть или блокировка)"
  fi

  [ $((loops % per_min)) -eq 0 ] && count_bots

  if [ "$(date -u +%H)" = "$SUMMARY_HOUR_UTC" ] && [ ! -f "$OUT/.summary_$day" ]; then
    : > "$OUT/.summary_$day"; daily_summary
    find "$OUT" -maxdepth 1 -name '*_20*.csv' -mtime +14 -delete 2>/dev/null
  fi

  loops=$((loops + 1))
  if [ "$MAX_LOOPS" -gt 0 ] && [ "$loops" -ge "$MAX_LOOPS" ]; then break; fi
  sleep "$SAMPLE_SEC"
done
