#!/bin/bash
set -euo pipefail

cd /opt/trading-bot

echo "=== [1/5] Останавливаю всех ботов ==="
curl -fsS -X POST http://localhost:5000/api/bots/stop-all || true
sleep 5

echo "=== [2/5] Очищаю биржу ==="
sudo docker cp scripts/flatten_exchange.py replit_scalper-api-testnet:/app/flatten_exchange.py
sudo docker compose exec api-testnet bash -lc 'source /app/.env && python3 /app/flatten_exchange.py --confirm' || true

echo "=== [3/5] Чищу БД testnet ==="
sudo python3 -c "import sqlite3, datetime, shutil; ts=datetime.datetime.now().strftime('%Y%m%d_%H%M%S'); shutil.copy('data/bot.db', 'data/bot.db.bak.' + ts); conn=sqlite3.connect('data/bot.db'); c=conn.cursor(); c.execute('DELETE FROM trades'); c.execute(\"DELETE FROM sqlite_sequence WHERE name='trades'\"); conn.commit(); conn.close(); print('DB cleared')"

echo "=== [4/5] Перезапускаю ботов ==="
./deploy/oci/restart_bots.sh testnet

echo "=== [5/5] Проверка ==="
curl -fsS http://localhost:5000/api/bots | python3 -c "import sys,json; d=json.load(sys.stdin); r=[b for b in d if b.get('is_running')]; print('БД:', len(d), '| Запущено:', len(r))"
