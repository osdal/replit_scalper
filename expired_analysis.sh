#!/bin/bash
cd /opt/trading-bot
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY'
import sqlite3, datetime, os, asyncio
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient

con = sqlite3.connect('/app/data/bot.db'); con.row_factory = sqlite3.Row

def hold_h(r):
    try:
        a = datetime.datetime.fromisoformat(str(r['entry_time']).replace('Z','+00:00'))
        b = datetime.datetime.fromisoformat(str(r['exit_time']).replace('Z','+00:00'))
        return (b-a).total_seconds()/3600
    except Exception:
        return None

print("=== EXPIRED-закрытия с 03.11 ===")
rows = con.execute("""SELECT symbol,direction,entry_price,exit_price,pnl,exit_reason,entry_time,exit_time
FROM trades WHERE is_open=0 AND exit_reason IN ('EXPIRED_CLOSE','REVERSE_EXPIRED_PROFIT')
AND exit_time >= '2026-11-03' ORDER BY exit_time""").fetchall()
for r in rows:
    h = hold_h(r)
    print(f"{r['exit_time'][:16]} {r['symbol']:<9} {r['direction']:<5} "
          f"hold={h:5.1f}h pnl={float(r['pnl'] or 0):+.4f} {r['exit_reason']}")
print("всего:", len(rows), "| сумма:", round(sum(float(r['pnl'] or 0) for r in rows),4))

print("\n=== причины закрытий с 03.11: сколько держали ===")
from collections import defaultdict
agg = defaultdict(lambda: [0, 0.0, 0.0])
for r in con.execute("""SELECT exit_reason,pnl,entry_time,exit_time FROM trades
WHERE is_open=0 AND exit_time >= '2026-11-03'"""):
    h = hold_h(r)
    a = agg[r['exit_reason'] or '?']
    a[0] += 1; a[1] += float(r['pnl'] or 0); a[2] += (h or 0)
for k, (n, pnl, hs) in sorted(agg.items(), key=lambda x: -x[1][0]):
    print(f"{k:<26} n={n:3d} avg_hold={hs/n:5.1f}h sum={pnl:+.4f}")

print("\n=== открытые: возраст, notional, требуемый порог, текущий upnl ===")
rows = con.execute("""SELECT id,symbol,direction,qty,entry_price,entry_time,is_reverse
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time""").fetchall()
now = datetime.datetime.now(datetime.timezone.utc)
ASYNC = None
async def fetch():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        return {p['symbol']: float(p.get('unRealizedProfit',0) or 0) for p in await c.futures_position_information()}
    finally:
        await c.close_connection()
upnl = asyncio.run(fetch())
for r in rows:
    et = datetime.datetime.fromisoformat(str(r['entry_time']).replace('Z','+00:00'))
    if et.tzinfo is None: et = et.replace(tzinfo=datetime.timezone.utc)
    age = (now-et).total_seconds()/3600
    notional = abs(float(r['qty'])) * float(r['entry_price'])
    req = notional * 0.0005 + 0.02
    u = upnl.get(r['symbol'], 0.0)
    print(f"{r['symbol']:<9} {r['direction']:<5} age={age:5.1f}h notional=${notional:6.2f} "
          f"required=${req:.4f} upnl={u:+.4f} "
          f"{'-> ЗАКРЫТЬ' if (age>=2 and u>=req) else ('wait +%.4f' % (req-u) if age>=2 else 'молодая')}")
PY
