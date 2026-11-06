#!/bin/bash
cd /opt/trading-bot/bot/logs/testnet
grep -hE "\-1003|\-429" api_*.log 2>/dev/null | grep "2026-11-03" | tail -12
