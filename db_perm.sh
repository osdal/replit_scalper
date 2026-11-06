#!/bin/bash
cd /opt/trading-bot
id
ls -l data/bot.db data/ 2>/dev/null | head -10
echo "--- mount в контейнере ---"
docker inspect replit_scalper-api-testnet --format '{{range .Mounts}}{{.Source}} -> {{.Destination}} ({{.Mode}}){{"\n"}}{{end}}' 2>/dev/null
echo "--- записываемость из контейнера ---"
docker exec replit_scalper-api-testnet bash -lc 'ls -l /app/data/bot.db 2>/dev/null; id; python3 -c "import sqlite3;c=sqlite3.connect(\"/app/data/bot.db\");print(c.execute(\"select count(*) from trades where is_open=1\").fetchone()[0]);c.close()"' 2>&1 | tail -5
