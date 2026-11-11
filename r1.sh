#!/bin/bash
date -u
echo "=== procs ==="
docker exec replit_scalper-api-testnet bash -lc "ps aux | grep python3 | grep -v grep | wc -l"
echo "=== container ==="
docker ps --filter name=replit_scalper-api-testnet --format '{{.Names}} {{.Status}}'
echo "=== bots table ==="
python3 -c "import sqlite3;c=sqlite3.connect('/opt/trading-bot/data/bot.db');print([r[1] for r in c.execute('pragma table_info(trades)')])"
echo "=== bots is_running ==="
python3 -c "import sqlite3;c=sqlite3.connect('/opt/trading-bot/data/bot.db');r=[x[0] for x in c.execute('select symbol from bots where is_running=1 order by symbol')];print(len(r), r)"
echo "=== DB mtime / size ==="
ls -l /opt/trading-bot/data/bot.db
