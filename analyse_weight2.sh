#!/bin/bash
D=$(date -u +%F)
echo "=== mainnet weight csv today (last 40 lines) ==="
tail -40 /opt/trading-bot/logs/monitor/weight_$D.csv 2>/dev/null
echo
echo "=== stats today ==="
awk -F, '$2 ~ /^[0-9]+$/ {n++; s+=$2; if($2>m)m=$2; if($2>=1000)hi++} END{printf "samples=%d avg=%.0f max=%d >=1000:%d\n", n, s/n, m, hi+0}' /opt/trading-bot/logs/monitor/weight_$D.csv 2>/dev/null
echo
echo "=== alerts today ==="
grep "$D" /opt/trading-bot/logs/monitor/alerts.log 2>/dev/null | tail -10
echo
echo "=== mainnet weight sample now ==="
curl -s -m 8 -D - -o /dev/null https://fapi.binance.com/fapi/v1/ping 2>/dev/null | tr -d '\r' | grep -i "x-mbx-used-weight"
echo
echo "=== testnet weight sample now ==="
curl -s -m 8 -D - -o /dev/null https://testnet.binancefuture.com/fapi/v1/ping 2>/dev/null | tr -d '\r' | grep -iE "x-mbx-used-weight|^HTTP"
echo
echo "=== testnet requests from host: 60s tcpdump on eth0 to testnet IPs ==="
timeout 62 tcpdump -i any -nn -q 'port 443 and (host 65.8.131.101 or host 65.8.131.115 or host 65.8.131.16 or host 65.8.131.86)' 2>/dev/null | tail -3
echo "packets to testnet (count):"
timeout 60 tcpdump -i any -nn -q 'port 443 and (host 65.8.131.101 or host 65.8.131.115 or host 65.8.131.16 or host 65.8.131.86)' 2>/dev/null | wc -l
