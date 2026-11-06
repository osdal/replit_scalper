#!/bin/bash
cd /opt/trading-bot
echo "=== A. Закрытые сделки за 03.11 ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
rows = con.execute("""SELECT symbol, exit_reason, ROUND(pnl,4) pnl, ROUND(commission,6) comm,
ROUND(qty*entry_price,2) notional, ROUND(chain_depth,0) chain, entry_time, exit_time
FROM trades WHERE exit_time >= '2026-11-03' AND is_open=0 AND status!='rejected' ORDER BY exit_time""").fetchall()
for r in rows:
    print(f"{r['exit_time'][11:19]} {r['symbol']:<10} {r['exit_reason']:<22} pnl={r['pnl']:>9} comm={r['comm']} n=${r['notional']} chain={int(r['chain'] or 0)}")
print("---")
print("итого:", len(rows), "| pnl=", round(sum(r['pnl'] or 0 for r in rows),4),
      "| wins", sum(1 for r in rows if (r['pnl'] or 0)>0), "| losses", sum(1 for r in rows if (r['pnl'] or 0)<0),
      "| comm=", round(sum(r['comm'] or 0 for r in rows),4))
sub = [r for r in rows if r['entry_time'] >= '2026-11-03T11:00']
print("$10-режим (вход >=11:00):", len(sub), "| pnl=", round(sum(r['pnl'] or 0 for r in sub),4),
      "| comm=", round(sum(r['comm'] or 0 for r in sub),4),
      "| wins", sum(1 for r in sub if (r['pnl'] or 0)>0))
PY
echo
echo "=== B. Открытые: notional / возраст / unrealized на бирже ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
for r in con.execute("""SELECT id,symbol,direction,qty,entry_price,entry_time FROM trades
WHERE is_open=1 AND status!='rejected' ORDER BY entry_time"""):
    et = datetime.datetime.fromisoformat(r['entry_time'])
    age = (now - et.replace(tzinfo=datetime.timezone.utc)).total_seconds()/3600
    print(f"{r['symbol']:<10} {r['direction']:<5} id={r['id']} qty={r['qty']} entry={r['entry_price']} "
          f"notional=${r['qty']*r['entry_price']:.2f} age={age:.1f}h")
PY
echo
echo "=== C. Отказы с 11:06 (по причинам) ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
for r in con.execute("""SELECT reject_reason, COUNT(*) FROM trades WHERE status='rejected'
AND entry_time >= '2026-11-03T11:06:00' GROUP BY 1 ORDER BY 2 DESC"""):
    print(r)
PY
echo
echo "=== D. LINK: позиция + minNotional + ордера ==="
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -25
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        info = await c.futures_exchange_info()
        filt = {s['symbol']: {f['filterType']: f for f in s['filters']} for s in info['symbols']}
        for p in await c.futures_position_information():
            if abs(float(p.get('positionAmt',0) or 0)) > 0:
                s = p['symbol']
                amt = float(p['positionAmt']); px = float(p.get('entryPrice') or 0)
                print(f"POS {s:<10} amt={amt} entry={px} notional=${abs(amt)*px:.2f} upnl={p.get('unRealizedProfit')} "
                      f"minNotional={filt.get(s,{}).get('MIN_NOTIONAL',{}).get('notional')}")
        print("--- orders (LINK) ---")
        for o in await c.futures_get_open_orders(symbol='LINKUSDT'):
            print(' ORD', {k: o.get(k) for k in ('type','side','price','stopPrice','origQty','reduceOnly','closePosition','status')})
        try:
            for a in await c.futures_get_open_algo_orders(symbol='LINKUSDT') or []:
                print(' ALGO', {k: a.get(k) for k in ('algoType','orderType','side','triggerPrice','closePosition','algoId')})
        except Exception as e:
            print(' ALGO ERR', e)
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== E. LINK state ==="
cat bot/state_testnet_linkusdt.json
echo
echo "=== F. daily_loss / bots / docker ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db')
con.row_factory = sqlite3.Row
print("daily_loss:", round(con.execute("SELECT COALESCE(SUM(-pnl),0) FROM trades WHERE is_open=0 AND pnl<0 AND exit_time >= '2026-11-03'").fetchone()[0],4))
now = datetime.datetime.now(datetime.timezone.utc)
rows = con.execute("SELECT symbol,is_running,armed,last_heartbeat FROM bots").fetchall()
bad=[]
for r in rows:
    try:
        et = datetime.datetime.fromisoformat(str(r['last_heartbeat']).replace('Z','+00:00'))
        if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
        age=(now-et).total_seconds()/60
    except Exception:
        age=999
    if not r['is_running'] or not r['armed'] or age>10: bad.append((r['symbol'],r['is_running'],r['armed'],round(age,1)))
print("bots:", len(rows), "проблемных:", len(bad), bad[:5])
PY
docker ps --filter name=replit_scalper-api-testnet --format '{{.Status}}'
curl -s -X POST http://localhost:5000/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"ETHUSDT"}'
echo
