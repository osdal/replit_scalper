#!/bin/bash
cd /opt/trading-bot
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY'
import sqlite3, datetime, os, asyncio
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient

con = sqlite3.connect('/app/data/bot.db'); con.row_factory = sqlite3.Row
rows = con.execute("""SELECT symbol,direction,qty,entry_price,entry_time
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY entry_time""").fetchall()

def ms(s):
    d = datetime.datetime.fromisoformat(str(s).replace('Z','+00:00'))
    if d.tzinfo is None: d = d.replace(tzinfo=datetime.timezone.utc)
    return int(d.timestamp()*1000)

async def main():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None, testnet=True)
    try:
        now = int(datetime.datetime.now(datetime.timezone.utc).timestamp()*1000)
        for r in rows:
            e_ms = ms(r['entry_time'])
            kl = await c.futures_klines(symbol=r['symbol'], interval='5m', startTime=e_ms, limit=1000)
            if not kl:
                print(r['symbol'], "нет свечей"); continue
            px = [float(k[4]) for k in kl]          # close
            ts = [int(k[6]) for k in kl]            # closeTime
            qty = abs(float(r['qty'])); entry = float(r['entry_price'])
            sign = 1.0 if r['direction'] == 'LONG' else -1.0
            def pnl(i): return (px[i]-entry)*qty*sign
            def at(h):
                target = e_ms + int(h*3600*1000)
                idx = [i for i,t in enumerate(ts) if t >= target]
                return pnl(idx[0]) if idx else None
            # окно после протухания (2ч)
            i2 = [i for i,t in enumerate(ts) if t >= e_ms + 2*3600*1000]
            if i2:
                after = [pnl(i) for i in range(i2[0], len(ts))]
                mfe, mae = max(after), min(after)
            else:
                mfe = mae = None
            req = abs(qty*entry)*0.0005 + 0.02
            cur = pnl(len(px)-1)
            age = (now-e_ms)/3600000
            f = lambda v: "n/a" if v is None else f"{v:+.4f}"
            print(f"{r['symbol']:<9} {r['direction']:<5} age={age:5.1f}h | "
                  f"2h={f(at(2))} 4h={f(at(4))} 6h={f(at(6))} 12h={f(at(12))} now={f(cur)} | "
                  f"MFE(после 2ч)={f(mfe)} MAE={f(mae)} порог={req:.4f} "
                  f"{'> ДОСТИЖИМ' if mfe is not None and mfe >= req else '> НЕ БЫЛО'}")
    finally:
        await c.close_connection()

asyncio.run(main())
PY
