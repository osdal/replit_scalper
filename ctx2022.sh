#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet
echo "=== Контекст вокруг -2022 серии OPUSDT 06:15:50-06:16:10 ==="
grep -E "2026-10-31 06:15:(4[5-9]|5[0-9])|2026-10-31 06:16:0[0-9]" api_opusdt_stdout.log | head -60
