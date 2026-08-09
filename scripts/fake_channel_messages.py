import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "bot"))

from notifier import Notifier
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))


async def main():
    notifier = Notifier()
    if not notifier.bot:
        print("Telegram not configured")
        sys.exit(1)

    # 10 fake messages: 6 positive, 4 negative
    messages = [
        # Positive trades
        ("signal", {
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "entry_price": 65000.0,
            "sl_price": 64500.0,
            "tp1_price": 66000.0,
            "tp2_price": 67000.0,
            "leverage": 10,
        }),
        ("event", {
            "event_type": "tp1_hit",
            "symbol": "BTCUSDT",
            "direction": "LONG",
            "entry_price": 65000.0,
            "exit_price": 66000.0,
            "pnl": 1.54,
        }),
        ("signal", {
            "symbol": "ETHUSDT",
            "direction": "SHORT",
            "entry_price": 2500.0,
            "sl_price": 2550.0,
            "tp1_price": 2400.0,
            "tp2_price": 2300.0,
            "leverage": 5,
        }),
        ("event", {
            "event_type": "tp2_hit",
            "symbol": "ETHUSDT",
            "direction": "SHORT",
            "entry_price": 2500.0,
            "exit_price": 2300.0,
            "pnl": 8.0,
        }),
        ("signal", {
            "symbol": "SOLUSDT",
            "direction": "LONG",
            "entry_price": 145.0,
            "sl_price": 142.0,
            "tp1_price": 150.0,
            "tp2_price": 155.0,
            "leverage": 10,
        }),
        ("event", {
            "event_type": "tp1_hit",
            "symbol": "SOLUSDT",
            "direction": "LONG",
            "entry_price": 145.0,
            "exit_price": 150.0,
            "pnl": 3.45,
        }),
        ("signal", {
            "symbol": "BNBUSDT",
            "direction": "LONG",
            "entry_price": 580.0,
            "sl_price": 572.0,
            "tp1_price": 595.0,
            "tp2_price": 610.0,
            "leverage": 5,
        }),
        ("event", {
            "event_type": "tp1_hit",
            "symbol": "BNBUSDT",
            "direction": "LONG",
            "entry_price": 580.0,
            "exit_price": 595.0,
            "pnl": 2.59,
        }),
        ("signal", {
            "symbol": "XRPUSDT",
            "direction": "SHORT",
            "entry_price": 0.62,
            "sl_price": 0.625,
            "tp1_price": 0.60,
            "tp2_price": 0.58,
            "leverage": 10,
        }),
        ("event", {
            "event_type": "tp1_hit",
            "symbol": "XRPUSDT",
            "direction": "SHORT",
            "entry_price": 0.62,
            "exit_price": 0.60,
            "pnl": 3.23,
        }),
        # Negative trades
        ("signal", {
            "symbol": "POLUSDT",
            "direction": "LONG",
            "entry_price": 0.075,
            "sl_price": 0.073,
            "tp1_price": 0.078,
            "tp2_price": 0.081,
            "leverage": 10,
        }),
        ("event", {
            "event_type": "sl_hit",
            "symbol": "POLUSDT",
            "direction": "LONG",
            "entry_price": 0.075,
            "exit_price": 0.073,
            "pnl": -2.67,
        }),
        ("signal", {
            "symbol": "KASUSDT",
            "direction": "SHORT",
            "entry_price": 0.12,
            "sl_price": 0.122,
            "tp1_price": 0.115,
            "tp2_price": 0.11,
            "leverage": 5,
        }),
        ("event", {
            "event_type": "sl_hit",
            "symbol": "KASUSDT",
            "direction": "SHORT",
            "entry_price": 0.12,
            "exit_price": 0.122,
            "pnl": -1.67,
        }),
        ("signal", {
            "symbol": "SUIUSDT",
            "direction": "LONG",
            "entry_price": 1.05,
            "sl_price": 1.03,
            "tp1_price": 1.08,
            "tp2_price": 1.12,
            "leverage": 5,
        }),
        ("event", {
            "event_type": "sl_hit",
            "symbol": "SUIUSDT",
            "direction": "LONG",
            "entry_price": 1.05,
            "exit_price": 1.03,
            "pnl": -1.90,
        }),
        ("signal", {
            "symbol": "AVAXUSDT",
            "direction": "SHORT",
            "entry_price": 35.0,
            "sl_price": 36.0,
            "tp1_price": 32.0,
            "tp2_price": 30.0,
            "leverage": 5,
        }),
        ("event", {
            "event_type": "sl_hit",
            "symbol": "AVAXUSDT",
            "direction": "SHORT",
            "entry_price": 35.0,
            "exit_price": 36.0,
            "pnl": -2.86,
        }),
    ]

    print(f"Sending {len(messages)} messages to Telegram...")
    for i, (msg_type, data) in enumerate(messages, 1):
        if msg_type == "signal":
            notifier.send_signal(data)
            print(f"  [{i}/10] Signal {data['symbol']} {data['direction']}")
        elif msg_type == "event":
            event_type = data.pop("event_type")
            notifier.send_event(event_type, data)
            print(f"  [{i}/10] Event {event_type} {data['symbol']}")
        await asyncio.sleep(1.5)

    await asyncio.sleep(3)
    print("Done")


if __name__ == "__main__":
    asyncio.run(main())
