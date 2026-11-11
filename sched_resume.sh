#!/bin/bash
# Планировщик на СЕРВЕРЕ: переживает выключение локального ПК.
set -u
TARGET=$(date -u -d '2026-11-10 12:37:00' +%s)
NOW=$(date -u +%s)
WAIT=$(( TARGET - NOW ))
[ "$WAIT" -lt 0 ] && WAIT=0
echo "scheduler started $(date -u) wait_s=$WAIT"
sleep "$WAIT"
echo "=== running check at $(date -u) ==="
bash /tmp/check_resume.sh 2>&1
echo "=== done $(date -u) ==="
