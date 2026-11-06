#!/bin/bash
cd /opt/trading-bot
date -u
python3 -c "import datetime; ts=1793878139995/1000; print(datetime.datetime.fromtimestamp(ts, datetime.timezone.utc))"
echo "=== check gate state files ==="
ls -la /opt/trading-bot/data/binance_gate* 2>&1