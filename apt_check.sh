#!/bin/bash
cd /opt/trading-bot
date -u
echo
echo "=== 1. APTUSDT: строки в БД ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
for r in con.execute("""SELECT id,symbol,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,
ROUND(exit_price,6) exit_price,ROUND(pnl,4) pnl,ROUND(commission,6) commission,is_open,status,
exit_reason,entry_time,exit_time FROM trades WHERE symbol='APTUSDT' ORDER BY id DESC LIMIT 8"""):
    print(dict(r))
PY
echo
echo "=== 2. APTUSDT: лог (закрытия/TP/SL/ошибки/EXPIRED) ==="
grep -hE "TP1|TP2|TP_CLOSE|stop|STOP|SL|EXPIRED|REVERSE|Market order placed|Position closed|CLOSED" bot/logs/testnet/api_aptusdt_stdout.log 2>/dev/null | tail -30
echo
echo "=== 3. Все открытые accepted в БД ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
rows = con.execute("""SELECT id,symbol,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,
ROUND(qty*entry_price,2) notional,ROUND(pnl,4) pnl,entry_time,status
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time""").fetchall()
for r in rows:
    d = dict(r)
    et = datetime.datetime.fromisoformat(d['entry_time'].replace('Z','+00:00'))
    if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
    d['age_h'] = round((now-et).total_seconds()/3600, 2)
    print(d)
print("всего открытых:", len(rows))
PY
echo
echo "=== 4. Позиции и ордера на бирже (все ненулевые) ==="
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -20
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        for p in await c.futures_position_information():
            amt = float(p.get('positionAmt', 0) or 0)
            if abs(amt) > 0:
                print('POS', p['symbol'], amt, p.get('entryPrice'), 'upnl', p.get('unRealizedProfit'))
        for s in ('APTUSDT','DOGEUSDT','MTLUSDT'):
            try:
                o = await c.futures_get_open_orders(symbol=s)
                print('ORD', s, [(x['type'], x['side'], x['price'], x['stopPrice'], x['qty']) for x in o])
            except Exception as e:
                print('ORD', s, 'ERR', e)
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== 5. /trading/check ETH ==="
curl -s -X POST http://localhost:5000/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"ETHUSDT"}'
echo
echo "=== 6. Закрытия за 03.11 ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
rows = con.execute("""SELECT symbol,exit_reason,ROUND(pnl,4) pnl,ROUND(qty*entry_price,2) notional,exit_time
FROM trades WHERE exit_time >= '2026-11-03' AND is_open=0 AND status!='rejected' ORDER BY exit_time""").fetchall()
for r in rows: print(dict(r))
print("итого:", len(rows), "pnl=", round(sum(r['pnl'] or 0 for r in rows), 4))
PY
echo
echo "=== 7. ERROR 03.11 ==="
grep -h "ERROR" bot/logs/testnet/api_*.log 2>/dev/null | grep -c "2026-11-03"
echo
echo "=== 8. Боты ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
rows = con.execute("SELECT symbol,is_running,armed,last_heartbeat FROM bots").fetchall()
bad = []
for r in rows:
    ts = r['last_heartbeat']; age = None
    if ts:
        try:
            et = datetime.datetime.fromisoformat(str(ts).replace('Z','+00:00'))
            if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
            age = (now-et).total_seconds()/60
        except Exception: pass
    if not r['is_running'] or not r['armed'] or age is None or age > 10:
        bad.append((r['symbol'], r['is_running'], r['armed'], ts))
print("всего:", len(rows), "проблемных:", len(bad))
for b in bad[:10]: print("  ", b)
PY
