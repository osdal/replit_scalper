#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet || exit 1
show() {
  local f="$1" start="$2" end="$3"
  echo "===== $f  $start .. $end ====="
  awk -v s="$start" -v e="$end" '
    { line=$0; ts=substr(line,1,19);
      if (ts >= s && ts <= e) print line }' "$f" \
    | grep -avE '\[CACHE\] using cached position|IP banned -1003 \(same ban\)|\[RATE_LIMIT\] demand' \
    | tail -40
  echo
}
show api_filusdt_stdout.log    "2026-11-10 09:46:30" "2026-11-10 09:50:30"
show api_dogeusdt_stdout.log   "2026-11-10 07:29:00" "2026-11-10 07:32:30"
show api_dogeusdt_stdout.log   "2026-11-10 04:27:00" "2026-11-10 04:31:00"
show api_adausdt_stdout.log    "2026-11-10 00:06:30" "2026-11-10 00:11:00"
show api_lskusdt_stdout.log    "2026-11-09 22:17:00" "2026-11-09 22:21:30"
show api_lskusdt_stdout.log    "2026-11-08 21:45:00" "2026-11-08 21:49:30"
show api_lskusdt_stdout.log    "2026-11-07 21:41:00" "2026-11-07 21:46:00"
