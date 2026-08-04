import sqlite3
import os
import sys
import asyncio
from datetime import datetime, timezone
from binance import AsyncClient
from dotenv import load_dotenv

DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "bot.db")
load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


async def fetch_klines(client, symbol, start_ms, limit=1000):
    klines = await client.futures_klines(
        symbol=symbol,
        interval="5m",
        startTime=int(start_ms),
        limit=limit,
    )
    return klines


def simulate(direction, entry_price, sl_price, tp1_price, klines):
    direction = direction.upper()
    for k in klines:
        open_time = int(k[0])
        high = float(k[2])
        low = float(k[3])
        close = float(k[4])

        if direction == "LONG":
            if low <= sl_price:
                return "SL", sl_price, open_time
            if high >= tp1_price:
                return "TP1", tp1_price, open_time
        else:
            if high >= sl_price:
                return "SL", sl_price, open_time
            if low <= tp1_price:
                return "TP1", tp1_price, open_time
    return None, None, None


async def main():
    client = await AsyncClient.create(
        os.getenv("BINANCE_API_KEY", ""),
        os.getenv("BINANCE_API_SECRET", ""),
    )

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        """
        SELECT id, symbol, direction, entry_price, sl_price, tp1_price, entry_time
        FROM trades
        WHERE status = 'rejected' AND entry_time IS NOT NULL
          AND exit_reason IS NULL
        ORDER BY entry_time DESC
        """
    )
    rows = c.fetchall()
    print(f"Found {len(rows)} rejected trades to simulate")

    updated = 0
    skipped = 0
    errors = 0

    for row in rows:
        trade_id, symbol, direction, entry_price, sl_price, tp1_price, entry_time = row
        try:
            dt = datetime.fromisoformat(entry_time.replace("Z", "+00:00"))
            start_ms = int(dt.timestamp() * 1000)
        except Exception as e:
            print(f"  [{trade_id}] bad entry_time: {entry_time} ({e})")
            skipped += 1
            continue

        klines = await fetch_klines(client, symbol, start_ms, limit=500)
        if not klines:
            print(f"  [{trade_id}] no klines for {symbol} from {entry_time}")
            skipped += 1
            continue

        exit_reason, exit_price, exit_open_time = simulate(
            direction, entry_price, sl_price, tp1_price, klines
        )

        if exit_reason:
            exit_time = datetime.fromtimestamp(exit_open_time / 1000, tz=timezone.utc).isoformat()
            if direction.upper() == "LONG":
                pnl_pct = (exit_price - entry_price) / entry_price * 100.0
            else:
                pnl_pct = (entry_price - exit_price) / entry_price * 100.0
            pnl = round(pnl_pct, 4)
            c.execute(
                """
                UPDATE trades
                SET exit_reason = ?, exit_price = ?, pnl = ?, exit_time = ?
                WHERE id = ?
                """,
                (exit_reason, exit_price, pnl, exit_time, trade_id),
            )
            conn.commit()
            print(f"  [{trade_id}] {symbol} {direction} => {exit_reason} at {exit_price} ({pnl:+.2f}%)")
            updated += 1
        else:
            skipped += 1

    conn.close()
    await client.close_connection()
    print(f"\nSimulation complete: updated={updated}, skipped={skipped}, errors={errors}")


if __name__ == "__main__":
    asyncio.run(main())
