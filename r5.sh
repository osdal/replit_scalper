#!/bin/bash
date -u
echo "=== latest balance lines ==="
grep -h "cached balance \$" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | tail -5
grep -h "Wallet balance\|walletBalance\|free balance" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | tail -5
echo
echo "=== daily loss limit mentions ==="
grep -hE "daily.?loss|DAILY_LOSS|kill.?switch" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "^2026-11-(07|08|09|10)" | tail -8
echo
echo "=== gate stats (last lines) ==="
grep -h "GATE.*weight\|gate.*weight\|GateStats\|shed" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | tail -6 | cut -c1-160
echo
echo "=== rejected reasons (Nov 8-10) ==="
python3 - << 'EOF'
import sqlite3
c = sqlite3.connect('/opt/trading-bot/data/bot.db')
q = """select coalesce(reject_reason,'?') r, count(*) n from trades
       where entry_time >= '2026-11-08 00:00:00' and status='rejected'
       group by r order by n desc limit 12"""
for r in c.execute(q):
    print(f"{str(r[0])[:60]:62} {r[1]}")
EOF
echo
echo "=== loss streak / pause events ==="
grep -h "loss_streak\|PAUSE\|paused" /opt/trading-bot/bot/logs/testnet/*_stdout.log 2>/dev/null | grep -E "^2026-11-(08|09|10)" | tail -8 | cut -c1-150
