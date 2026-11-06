#!/bin/bash
cd /opt/trading-bot
echo "=== текущие открытые (возраст) ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,symbol,direction,qty,entry_price,entry_time
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time"""):
    et = datetime.datetime.fromisoformat(str(r['entry_time']).replace('Z','+00:00'))
    if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
    age = (datetime.datetime.now(datetime.timezone.utc)-et).total_seconds()/3600
    deadline = ">>6h DEADLINE" if age >= 6 else ("~2h expired" if age >= 2 else "active")
    print(f"{r['symbol']:<9} {r['direction']:<5} age={age:5.1f}h {deadline}")
PY
echo
echo "=== heartbeat ботов (должен быть свежий) ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
for r in con.execute("SELECT symbol,last_heartbeat FROM bots WHERE is_running=1 AND armed=1"):
    try:
        et = datetime.datetime.fromisoformat(str(r['last_heartbeat']).replace('Z','+00:00'))
        if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
        age = (now-et).total_seconds()
        if age > 30:
            print(f"{r['symbol']:<9} heartbeat {age:.0f}с назад")
    except:
        pass
PY
echo
echo "=== последние логи ETH (должен закрыться дедлайном) ==="
grep -h "ETHUSDT" bot/logs/testnet/api_ethusdt_stdout.log 2>/dev/null | tail -8