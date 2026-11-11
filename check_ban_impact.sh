#!/bin/bash
echo "=== open positions (DB) ==="
sqlite3 /opt/trading-bot/data/bot.db "select symbol, direction, datetime(entry_time,'unixepoch') as et, remaining_qty, status from trades where is_open=1 order by id desc limit 15;" 2>&1
echo ""
echo "=== rejected trades since 12:30 UTC ==="
sqlite3 /opt/trading-bot/data/bot.db "select count(*) from trades where is_open=0 and entry_time > strftime('%s','2026-11-07 12:30:00') and reason like '%max_positions%';" 2>&1
echo ""
echo "=== any trade rows created since 12:30 (opened/closed) ==="
sqlite3 /opt/trading-bot/data/bot.db "select id, symbol, status, datetime(entry_time,'unixepoch'), datetime(exit_time,'unixepoch'), reason from trades where entry_time > strftime('%s','2026-11-07 12:30:00') order by id desc limit 10;" 2>&1
echo ""
echo "=== exchange-side protection present? (last ensure logs) ==="
grep -h "ensure.*protect\|STOP_MARKET\|stop order" /opt/trading-bot/bot/logs/testnet/*.log 2>/dev/null | tail -5
echo ""
echo "=== sample bot activity 12:50-13:05 (BTC) ==="
tail -20 /opt/trading-bot/bot/logs/testnet/api_btcusdt_stdout.log 2>&1
