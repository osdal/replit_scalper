#!/bin/bash
set -e
cd /opt/trading-bot
docker exec -i -w /app/bot -e BOT_ENV=testnet replit_scalper-api-testnet python3 - <<'PY'
import asyncio, datetime, os, sqlite3
from dotenv import load_dotenv
load_dotenv('/app/.env', override=True)
from binance import AsyncClient

DB = "/app/data/bot.db"
now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat()

async def positions():
    c = await AsyncClient.create(api_key=os.getenv('BINANCE_API_KEY') or None,
                                 api_secret=os.getenv('BINANCE_API_SECRET') or None,
                                 testnet=True)
    try:
        snap = {}
        for p in await c.futures_position_information():
            a = float(p.get('positionAmt', 0) or 0)
            if abs(a) > 0:
                snap[p['symbol']] = a
        return snap
    finally:
        await c.close_connection()

snap = asyncio.run(positions())
print("биржа:", {k: v for k, v in sorted(snap.items())})

con = sqlite3.connect(DB, timeout=30)
con.row_factory = sqlite3.Row

# Бэкап перед правкой
ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
bak_path = f"{DB}.bak_phantom_{ts}"
bak = sqlite3.connect(bak_path)
con.backup(bak)
bak.close()
print("бэкап:", bak_path)

targets = [
    (204253, "NEARUSDT", "LONG", 1.0),
    (204603, "SUIUSDT", "SHORT", 3.6),
]

closed = 0
con.execute("BEGIN IMMEDIATE")
for tid, sym, d, q in targets:
    amt = snap.get(sym, 0.0)
    if (d == "LONG" and amt > 0) or (d == "SHORT" and amt < 0):
        print(f"id={tid}: на бирже есть {sym} {d} (amt={amt}) — НЕ трогаю")
        continue
    row = con.execute(
        "SELECT id,symbol,direction,qty,is_open FROM trades WHERE id=?", (tid,)
    ).fetchone()
    if not row:
        print(f"id={tid}: не найдена — пропуск")
        continue
    if not row["is_open"] or row["symbol"] != sym or row["direction"] != d:
        print(f"id={tid}: не совпала ({dict(row)}) — пропуск")
        continue
    con.execute(
        """UPDATE trades SET is_open=0, status='closed', exit_reason='reconcile_flat',
           exit_price=entry_price, pnl=0, commission=0, cycle_pnl=0, exit_time=?
           WHERE id=?""",
        (now, tid),
    )
    closed += 1
    print(f"закрыта фантомная строка id={tid} {sym} {d} qty={q}")
con.commit()

print("закрыто:", closed)
print("\n=== открытые строки после очистки ===")
for r in con.execute("""SELECT id,symbol,direction,ROUND(qty,4) qty,ROUND(entry_price,6) entry,entry_time
FROM trades WHERE is_open=1 AND status!='rejected' ORDER BY symbol,id"""):
    print(dict(r))
print("всего открытых:", con.execute(
    "SELECT COUNT(*) FROM trades WHERE is_open=1 AND status!='rejected'").fetchone()[0])
con.close()
PY
