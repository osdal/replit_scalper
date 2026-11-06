#!/bin/bash
cd /opt/trading-bot
echo "=== проверка REVERSE_EXPIRED_DEADLINE в position_tracker ==="
docker exec replit_scalper-api-testnet bash -lc 'grep -n "REVERSE_EXPIRED_DEADLINE\|expired_deadline" /app/bot/position_tracker.py | head -10'
echo
echo "=== текущие открытые позиции ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,symbol,direction,qty,entry_price,entry_time
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time"""):
    et = datetime.datetime.fromisoformat(str(r['entry_time']).replace('Z','+00:00'))
    if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
    age = (datetime.datetime.now(datetime.timezone.utc)-et).total_seconds()/3600
    print(f"{r['symbol']:<9} {r['direction']:<5} age={age:5.1f}h qty={r['qty']} entry={r['entry_price']}")
PY