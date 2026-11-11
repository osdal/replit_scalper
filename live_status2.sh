#!/bin/bash
cd /opt/trading-bot || exit 1
echo "=== live /api/trading/status ==="
curl -s -m 15 http://localhost:5001/api/trading/status | python3 -m json.tool 2>/dev/null | head -40
echo
echo "=== .env.live risk values ==="
grep -aE '^(MAX_POSITIONS|LIVE_DAILY_LOSS_LIMIT_USD|LIVE_MAX_DRAWDOWN_PCT|LIVE_DEPOSIT_USD|LIVE_DEFAULT_MARGIN_USD|REVERSE_LOSSCAP_ENABLED|REVERSE_LOSSCAP_PCT|GRACEFUL_STOP_MAX_MIN|AUTO_RESTART_BOTS)=' .env.live
