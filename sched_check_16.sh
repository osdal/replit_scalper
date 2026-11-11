#!/bin/bash
# Планировщик на сервере: ждёт до 16:05 UTC и проверяет статус
set -u
TARGET=$(date -u -d '2026-11-10 16:05:00' +%s)
NOW=$(date -u +%s)
WAIT=$(( TARGET - NOW ))
[ "$WAIT" -lt 0 ] && WAIT=0
echo "scheduler started $(date -u) wait_s=$WAIT"
sleep "$WAIT"
echo "=== running check at $(date -u) ==="
cd /opt/trading-bot
bash /tmp/check_post_fix2.sh 2>&1
echo "=== done $(date -u) ==="