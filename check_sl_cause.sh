#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== bots max_positions now ==="
sudo python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT symbol,max_positions,is_running FROM bots WHERE is_running=1 ORDER BY symbol"):
    print(r)
PY
echo
echo "=== SL exits Nov 11: context ==="
show() {
  f="$1"; start="$2"; end="$3"
  echo "----- $f $start..$end"
  awk -v s="$start" -v e="$end" '{ts=substr($0,1,19); if (ts>=s && ts<=e) print}' "bot/logs/testnet/$f" \
   | grep -avE '\[CACHE\] using cached position|IP banned -1003 \(same ban\)|\[RATE_LIMIT\] demand' | tail -25
  echo
}
show api_1000pepeusdt_stdout.log "2026-11-11 04:43:00" "2026-11-11 04:48:30"
show api_lskusdt_stdout.log    "2026-11-11 05:06:00" "2026-11-11 05:11:00"
show api_lskusdt_stdout.log    "2026-11-11 08:46:00" "2026-11-11 08:51:00"
show api_lskusdt_stdout.log    "2026-11-11 13:20:00" "2026-11-11 13:25:00"
show api_dogeusdt_stdout.log   "2026-11-11 17:30:00" "2026-11-11 17:36:00"