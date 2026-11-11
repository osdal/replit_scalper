#!/bin/bash
date -u
KEEP="ADAUSDT FILUSDT LSKUSDT NEARUSDT 1000PEPEUSDT TRXUSDT DOGEUSDT INJUSDT"
echo "KEEP: $KEEP"
echo ""
echo "=== stop all others (graceful) ==="
ALL=$(curl -s http://localhost:5000/api/bots | jq -r '.[].symbol' 2>/dev/null)
if [ -z "$ALL" ]; then echo "API not reachable or jq missing"; exit 1; fi
for s in $ALL; do
  skip=0
  for k in $KEEP; do if [ "$s" = "$k" ]; then skip=1; fi; done
  if [ "$skip" -eq 0 ]; then
    printf "%-14s " "$s"
    curl -s -X POST "http://localhost:5000/api/bots/$s/stop" | head -c 110
    echo ""
    sleep 0.2
  fi
done
echo ""
echo "=== state right after requests ==="
curl -s http://localhost:5000/api/bots | jq -r '.[] | select(.is_running==true) | .symbol' 2>/dev/null | sort | tr '\n' ' '
echo ""
