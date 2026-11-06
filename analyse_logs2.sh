#!/bin/bash
cd /opt/trading-bot
LOG=bot/logs/testnet
echo "=== 'no attribute reset' by date ==="
grep -h "object has no attribute 'reset'" $LOG/api_*.log 2>/dev/null | cut -c1-10 | sort | uniq -c
echo "=== 'no attribute should_close' by date ==="
grep -h "object has no attribute 'should_close'" $LOG/api_*.log 2>/dev/null | cut -c1-10 | sort | uniq -c
echo "=== first/last 'no attribute' ==="
grep -h "object has no attribute" $LOG/api_*.log 2>/dev/null | head -2
grep -h "object has no attribute" $LOG/api_*.log 2>/dev/null | tail -2
echo
echo "=== -4164 notional<50 by date ==="
grep -h "no smaller than 50" $LOG/api_*.log 2>/dev/null | cut -c1-10 | sort | uniq -c
echo "=== -4164 notional<50 by symbol (today) ==="
grep -h "no smaller than 50" $LOG/api_*.log 2>/dev/null | grep -oE "LIVE [A-Z0-9]+" | sort | uniq -c | sort -rn | head
echo
echo "=== -4509 TIF GTE by date ==="
grep -h "TIF GTE" $LOG/api_*.log 2>/dev/null | cut -c1-10 | sort | uniq -c
echo
echo "=== -1003 sync failures by hour (last 24h) ==="
grep -h "Failed to fetch positions" $LOG/api_*.log 2>/dev/null | grep -E "2026-11-0[56]" | cut -c1-13 | sort | uniq -c
echo
echo "=== distinct ban_until timestamps (last 24h) ==="
grep -hoE "banned until [0-9]+" $LOG/api_*.log 2>/dev/null | sort -u | tail -25
echo
echo "=== CumLossCap config ==="
grep -n "cum_loss\|CUM_LOSS" /opt/trading-bot/bot/recovery_config.yaml 2>/dev/null | head -20
echo
echo "=== EXPIRED env in container ==="
docker exec replit_scalper-api-testnet bash -lc 'env | grep -E "EXPIRED|CUM_LOSS|LOSS_STREAK|BINANCE_SHED|MAX_POSITIONS" | sort' 2>&1
