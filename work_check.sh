#!/bin/bash
cd /opt/trading-bot
date -u
echo
echo "=== 1. Открытые accepted (БД) ==="
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
echo "=== 2. Закрытые сделки за 03.11 ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
rows = con.execute("""SELECT symbol, exit_reason, ROUND(pnl,4) pnl, ROUND(commission,6) commission,
ROUND(qty*entry_price,2) notional, ROUND(chain_depth,0) chain, entry_time, exit_time
FROM trades WHERE exit_time >= '2026-11-03' AND is_open=0 AND status!='rejected' ORDER BY exit_time""").fetchall()
for r in rows: print(dict(r))
print("итого:", len(rows), "pnl=", round(sum(r['pnl'] or 0 for r in rows), 4),
      "| wins:", sum(1 for r in rows if (r['pnl'] or 0) > 0))
PY
echo
echo "=== 3. Позиции и ордера на бирже ==="
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
            o = await c.futures_get_open_orders(symbol=s)
            print('ORD', s, [(x.get('type'), x.get('side'), x.get('price'), x.get('stopPrice'), x.get('origQty'), x.get('closePosition')) for x in o])
            a = await c.futures_get_open_algo_orders(symbol=s)
            print('ALGO', s, [(x.get('algoType'), x.get('side'), x.get('triggerPrice'), x.get('closePosition')) for x in (a or [])])
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== 4. /trading/check ETH ==="
curl -s -X POST http://localhost:5000/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"ETHUSDT"}'
echo
echo "=== 5. Сигналы/входы/закрытия в логах после 11:40 ==="
grep -hE "2026-11-03 (1[1-9]|2[0-3]):" bot/logs/testnet/api_*.log 2>/dev/null | grep -E "Market order placed|Position opened|EXTERNAL_CLOSE|EXPIRED|REVERSE_MARKET|TP_CLOSE|SL hit" | tail -25
echo
echo "=== 6. Ошибки/баны после 11:40 ==="
echo "ERROR: $(grep -h "ERROR" bot/logs/testnet/api_*.log 2>/dev/null | grep -cE "2026-11-03 (1[1-9]|2[0-3]):")"
grep -h "ERROR" bot/logs/testnet/api_*.log 2>/dev/null | grep -E "2026-11-03 (1[1-9]|2[0-3]):" | sed -E 's/.*ERROR //' | sed -E 's/[0-9]+/N/g' | sort | uniq -c | sort -rn | head -8
echo "-2022: $(grep -h "\-2022" bot/logs/testnet/api_*.log 2>/dev/null | grep -c "2026-11-03")"
echo "-4164: $(grep -h "\-4164" bot/logs/testnet/api_*.log 2>/dev/null | grep -c "2026-11-03")"
echo "-1003 после 11:40: $(grep -h "\-1003" bot/logs/testnet/api_*.log 2>/dev/null | grep -cE "2026-11-03 (1[1-9]|2[0-3]):")"
echo "shed>0: $(grep -h "RATE_LIMIT" bot/logs/testnet/api_*.log 2>/dev/null | grep "2026-11-03" | grep -cE "shed=[1-9]")"
echo
echo "=== 7. Отказы за 03.11 по причинам ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("SELECT reject_reason, COUNT(*) FROM trades WHERE status='rejected' AND entry_time >= '2026-11-03T11:06:00' GROUP BY 1 ORDER BY 2 DESC"):
    print(r)
PY
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
print("daily_loss (сумма минусов за UTC-день):", round(con.execute("SELECT COALESCE(SUM(-pnl),0) FROM trades WHERE is_open=0 AND pnl<0 AND exit_time >= '2026-11-03'").fetchone()[0], 4))
PY
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'
