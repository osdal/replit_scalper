#!/bin/bash
cd /opt/trading-bot || exit 1
docker exec replit_scalper-api-live bash -lc 'env | grep -E "^BINANCE_(WEIGHT|GATE|SHED|BAN)|^MAX_POSITIONS=|^POST_BAN_|^DAILY_LOSS|^LOSS_STREAK|^EXPIRED_"' | sort
