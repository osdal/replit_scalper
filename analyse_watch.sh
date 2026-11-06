#!/bin/bash
echo "=== binance-watch service ==="
systemctl cat binance-watch.service 2>&1
echo
echo "=== status ==="
systemctl status binance-watch.service --no-pager 2>&1 | head -20
echo
echo "=== service script content ==="
EXEC=$(systemctl show -p ExecStart --value binance-watch.service)
echo "$EXEC"
for f in $(echo "$EXEC" | grep -oE '/[^ ]+\.(py|sh)' | sort -u); do
  echo "--- $f"; sed -n '1,200p' "$f"
done
echo
echo "=== process tree ==="
ps -eo pid,ppid,etime,cmd | grep -i "binance-watch\|watch" | grep -v grep | head -10
echo
echo "=== ss inside api-testnet container ==="
docker exec replit_scalper-api-testnet bash -lc 'which ss; ss -tn 2>/dev/null | grep -c ":443"' 2>&1
