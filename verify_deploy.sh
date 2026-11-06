#!/bin/bash
cd /opt/trading-bot
echo "=== процессы ботов в контейнере ==="
docker exec replit_scalper-api-testnet bash -lc 'ps -eo pid,etimes,args | grep -c "[p]ython3 main.py"; ps -eo pid,etimes,args | grep "[p]ython3 main.py" | head -3'
echo
echo "=== версия кода внутри контейнера ==="
docker exec replit_scalper-api-testnet bash -lc 'grep -c "TP_REJECT_COOLDOWN_SEC" /app/bot/order_manager.py; grep -c "leg below minNotional" /app/bot/order_manager.py; grep -c "hash(msg.get" /app/bot/market_data.py'
echo
echo "=== свежесть heartbeat (мин) ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
ages = []
for r in con.execute("SELECT symbol,last_heartbeat,is_running,armed FROM bots"):
    et = datetime.datetime.fromisoformat(str(r['last_heartbeat']).replace('Z','+00:00'))
    if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
    ages.append(((now-et).total_seconds()/60, r['symbol'], r['is_running'], r['armed']))
ages.sort(reverse=True)
print("худшие 5:", [(s, round(a,1), run, arm) for a, s, run, arm in ages[:5]])
print("медиана:", round(sorted(a for a,_,_,_ in ages)[len(ages)//2], 1), "мин")
PY
