import asyncio
import os
import sys

import pandas as pd
from binance import AsyncClient
from dotenv import load_dotenv

from config import load_config
from logger import get_logger
from market_data import get_recent_klines, start_kline_socket
from strategy import calculate_indicators, get_signal
from signal_handler import SignalHandler
from order_manager import OrderManager
from position_tracker import PositionTracker
from backtester import run_backtest

load_dotenv()


async def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)
    log = get_logger(log_file=cfg.log_file, mode=cfg.mode)

    log.info(f"Bot starting | mode={cfg.mode} symbol={cfg.symbol} tf={cfg.timeframe}")
    log.info(
        f"Config | leverage={cfg.leverage}x risk={cfg.risk_pct}% "
        f"SL={cfg.sl_pct}% TP1={cfg.tp1_pct}% TP2={cfg.tp2_pct}% auto={cfg.auto_mode}"
    )

    api_key = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")

    if cfg.mode == "live" and (not api_key or not api_secret):
        log.error("LIVE mode requires BINANCE_API_KEY and BINANCE_API_SECRET in .env")
        sys.exit(1)

    client = await AsyncClient.create(
        api_key=api_key or None,
        api_secret=api_secret or None,
    )

    try:
        if cfg.mode == "backtest":
            await run_backtest(cfg, client, log)
            return

        await _run_live_or_paper(cfg, client, log)

    finally:
        await client.close_connection()
        log.info("Bot stopped")


async def _run_live_or_paper(cfg, client: AsyncClient, log):
    tracker = PositionTracker(cfg, log)
    order_mgr = OrderManager(cfg, log, client=client if cfg.mode == "live" else None)
    handler = SignalHandler(cfg, log)

    df_buffer: pd.DataFrame = await get_recent_klines(
        client=client,
        symbol=cfg.symbol,
        interval=cfg.timeframe,
        limit=max(cfg.ema_slow * 3, 200),
    )
    df_buffer = calculate_indicators(df_buffer, cfg)
    log.info(f"Loaded {len(df_buffer)} historical candles for warm-up")

    async def on_candle(candle: pd.Series):
        nonlocal df_buffer

        new_row = pd.DataFrame([candle]).set_index("open_time")
        df_buffer = pd.concat([df_buffer, new_row]).tail(500)
        df_buffer = calculate_indicators(df_buffer, cfg)

        current_price = float(candle["close"])

        if tracker.has_open_position():
            hit = tracker.check(current_price)
            if hit:
                pos = tracker.position
                if hit == "TP1":
                    tp1_qty = round(pos.total_qty * cfg.tp1_close_pct / 100, 6)
                    await order_mgr.close_partial(pos.direction, tp1_qty, current_price, "TP1")
                else:
                    await order_mgr.close_full(pos.direction, pos.remaining_qty, current_price, hit)
                tracker.apply_hit(hit, current_price)
            return

        signal = get_signal(df_buffer, cfg)
        if signal is None:
            return

        confirmed = await handler.confirm(signal)
        if not confirmed:
            return

        entry_price = await order_mgr.open_position(signal)
        if entry_price is not None:
            signal.entry_price = entry_price
            tracker.open(signal, qty=_calc_qty_from_last_order(cfg, entry_price))

    def _calc_qty_from_last_order(cfg, entry_price):
        from order_manager import calc_quantity
        balance = cfg.paper_balance
        return round(
            calc_quantity(balance, cfg.risk_pct, cfg.sl_pct, entry_price, cfg.leverage), 3
        )

    log.info(f"Listening for candles | {cfg.symbol} {cfg.timeframe} ...")
    await start_kline_socket(client, cfg.symbol, cfg.timeframe, on_candle)


if __name__ == "__main__":
    asyncio.run(main())
