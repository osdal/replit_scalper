#!/bin/bash
# Ждём до указанного UTC-времени и запускаем проверку.
target=$(date -u -d '2026-11-07 13:51:00' +%s)
now=$(date -u +%s)
d=$((target - now))
echo "now=$(date -u) target=13:51 sleep=${d}s"
if [ "$d" -gt 0 ]; then sleep "$d"; fi
exec bash /tmp/verify_p2b.sh
