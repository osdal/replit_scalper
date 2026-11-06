#!/bin/bash
cd /opt/trading-bot
date -u
echo "=== 1. Закрытия с 03.11 21:00 по сейчас ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
rows = con.execute("""SELECT symbol,exit_reason,ROUND(pnl,4) pnl,ROUND(cycle_pnl,4) cycle,ROUND(chain_depth,0) chain,
ROUND(qty*entry_price,2) notional,exit_time FROM trades
WHERE is_open=0 AND status!='rejected' AND exit_time >= '2026-11-03T21:00:00' ORDER BY exit_time""").fetchall()
for r in rows:
    print(f"{r['exit_time'][:19]} {r['symbol']:<10} {r['exit_reason']:<24} pnl={r['pnl']:>9} cycle={r['cycle']} chain={int(r['chain'] or 0)} n=${r['notional']}")
print("итого:", len(rows), "pnl=", round(sum(r['pnl'] or 0 for r in rows),4),
      "wins", sum(1 for r in rows if (r['pnl'] or 0)>0), "losses", sum(1 for r in rows if (r['pnl'] or 0)<0))
PY
echo
echo "=== 2. Открытые (БД) ==="
python3 - <<'PY'
import sqlite3, datetime
con = sqlite3.connect('data/bot.db'); con.row_factory = sqlite3.Row
now = datetime.datetime.now(datetime.timezone.utc)
for r in con.execute("SELECT id,symbol,direction,qty,entry_price,entry_time FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time"):
    et = datetime.datetime.fromisoformat(r['entry_time']).replace(tzinfo=datetime.timezone.utc)
    print(f"{r['symbol']:<10} {r['direction']:<5} id={r['id']} qty={r['qty']} entry={r['entry_price']} "
          f"n=${r['qty']*r['entry_price']:.2f} age={(now-et).total_seconds()/3600:.1f}h")
PY
echo
echo "=== 3. Биржа: позиции + ордера (с защитой от -1003) ==="
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY' 2>&1 | tail -40
import asyncio, os
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient
from binance.exceptions import BinanceAPIException
async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        try:
            pos = await c.futures_position_information()
        except BinanceAPIException as e:
            print("BAN:", e.code, e.message); return
        for p in pos:
            amt = float(p.get('positionAmt', 0) or 0)
            if abs(amt) > 0:
                print(f"POS {p['symbol']:<10} amt={amt} entry={p['entryPrice']} notional=${abs(amt)*float(p['entryPrice']):.2f} upnl={p.get('unRealizedProfit')}")
        for p in pos:
            amt = float(p.get('positionAmt', 0) or 0)
            if abs(amt) <= 0: continue
            s = p['symbol']
            o = await c.futures_get_open_orders(symbol=s)
            tp = [(x['type'], x['side'], x['price'], x['origQty']) for x in o]
            a = await c.futures_get_open_algo_orders(symbol=s) or []
            st = [(x['orderType'], x['side'], x['triggerPrice']) for x in a]
            print(f"   {s}: TP={tp} SL={st}")
    finally:
        await c.close_connection()
asyncio.run(main())
PY
echo
echo "=== 4. Баны: сколько сейчас и когда последний ==="
grep -h "banned until" bot/logs/testnet/api_*.log | grep "2026-11-04" | tail -3
echo "кол-во банов 04.11: $(grep -h '1003' bot/logs/testnet/api_*.log | grep -c '2026-11-04')"
echo
echo "=== 5. -2022 / -4164 04.11 ==="
echo "-2022: $(grep -h '\-2022' bot/logs/testnet/api_*.log | grep -c '2026-11-04')"
echo "-4164: $(grep -h '\-4164' bot/logs/testnet/api_*.log | grep -c '2026-11-04')"
echo
echo "=== 6. daily_loss 04.11 / trading check ==="
python3 - <<'PY'
import sqlite3
con = sqlite3.connect('data/bot.db')
print("daily_loss 04.11:", round(con.execute("SELECT COALESCE(SUM(-pnl),0) FROM trades WHERE is_open=0 AND pnl<0 AND exit_time >= '2026-11-04'").fetchone()[0],4))
print("открытых:", con.execute("SELECT COUNT(*) FROM trades WHERE is_open=1 AND status!='rejected'").fetchone()[0])
PY
curl -s -m 10 -X POST http://localhost:5000/api/trading/check -H "Content-Type: application/json" -d '{"symbol":"ETHUSDT"}'
echo
echo "=== 7. LINK: последние события ==="
grep -hE "2026-11-04 0[2-3]:" bot/logs/testnet/api_linkusdt_stdout.log | grep -vE "RATE_LIMIT|PRICE\]|Heartbeat|Candle closed" | tail -12
echo
echo "=== 8. LINK state ==="
cat bot/state_testnet_linkusdt.json 2>/dev/null | head -30
