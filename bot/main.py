import asyncio
import os
import sys

import pandas as pd
from binance import AsyncClient
from dotenv import load_dotenv

from config import load_config
from logger import get_logger
from market_data import get_recent_klines, start_kline_socket, start_multi_kline_socket
from strategy import calculate_indicators, calculate_htf_indicators, get_signal, get_htf_trend_latest
from signal_handler import SignalHandler
from order_manager import OrderManager, calc_quantity
from position_tracker import PositionTracker
from backtester import run_backtest

load_dotenv()

HEARTBEAT_CANDLES = 3  # log alive message every N candles (~15min at 5m TF)


async def main():
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)
    log = get_logger(log_file=cfg.log_file, mode=cfg.mode)

    log.info(f"Bot starting | mode={cfg.mode} symbol={cfg.symbol} tf={cfg.timeframe}")
    log.info(
        f"Config | leverage={cfg.leverage}x risk={cfg.risk_pct}% "
        f"SL={cfg.sl_pct}% TP1={cfg.tp1_pct}% TP2={cfg.tp2_pct}% auto={cfg.auto_mode}"
    )
    if cfg.htf_enabled:
        log.info(f"HTF filter | {cfg.htf_timeframe} EMA{cfg.htf_ema_fast}/{cfg.htf_ema_slow}")

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
    log.info(f"Loaded {len(df_buffer)} candles for warm-up ({cfg.timeframe})")

    htf_buffer: pd.DataFrame = pd.DataFrame()
    if cfg.htf_enabled:
        htf_buffer = await get_recent_klines(
            client=client,
            symbol=cfg.symbol,
            interval=cfg.htf_timeframe,
            limit=max(cfg.htf_ema_slow * 3, 100),
        )
        htf_buffer = calculate_htf_indicators(htf_buffer, cfg)
        trend = get_htf_trend_latest(htf_buffer)
        log.info(
            f"Loaded {len(htf_buffer)} candles for HTF warm-up "
            f"({cfg.htf_timeframe}) | trend={trend}"
        )

    candle_count = [0]

    async def on_candle(candle: pd.Series):
        nonlocal df_buffer

        new_row = pd.DataFrame([candle]).set_index("open_time")
        df_buffer = pd.concat([df_buffer, new_row]).tail(500)
        df_buffer = calculate_indicators(df_buffer, cfg)

        current_price = float(candle["close"])
        candle_count[0] += 1

        if candle_count[0] % HEARTBEAT_CANDLES == 0:
            htf_trend_now = get_htf_trend_latest(htf_buffer) if cfg.htf_enabled else "off"
            log.info(
                f"Heartbeat | candles={candle_count[0]} "
                f"price={current_price:.2f} htf_trend={htf_trend_now}"
            )

        if tracker.has_open_position():
            hit = tracker.check(current_price)
            if hit:
                pos = tracker.position
                if hit == "TP1":
                    tp1_qty = round(pos.total_qty * cfg.tp1_close_pct / 100, 6)
                    await order_mgr.close_partial(pos.direction, tp1_qty, current_price, "TP1")
                    tracker.apply_hit(hit, current_price)
                    await order_mgr.move_sl_to_breakeven(pos.direction, pos.entry_price)
                else:
                    await order_mgr.close_full(pos.direction, pos.remaining_qty, current_price, hit)
                    tracker.apply_hit(hit, current_price)
            return

        # Get signal without HTF filter first
        raw_signal = get_signal(df_buffer, cfg)
        if raw_signal is None:
            return

        # Apply HTF filter with explicit logging
        if cfg.htf_enabled:
            htf_trend = get_htf_trend_latest(htf_buffer)
            if htf_trend is not None and raw_signal.direction != htf_trend:
                log.info(
                    f"Signal {raw_signal.direction} BLOCKED by HTF "
                    f"| htf_trend={htf_trend} entry={raw_signal.entry_price:.2f}"
                )
                return
        
        signal = raw_signal
        confirmed = await handler.confirm(signal)
        if not confirmed:
            return

        entry_price = await order_mgr.open_position(signal)
        if entry_price is not None:
            signal.entry_price = entry_price
            qty = round(calc_quantity(
                cfg.paper_balance, cfg.risk_pct, cfg.sl_pct, entry_price, cfg.leverage
            ), 3)
            tracker.open(signal, qty=qty)

    async def on_htf_candle(candle: pd.Series):
        nonlocal htf_buffer
        try:
            new_row = pd.DataFrame([candle]).set_index("open_time")
            htf_buffer = pd.concat([htf_buffer, new_row]).tail(300)
            htf_buffer = calculate_htf_indicators(htf_buffer, cfg)
            trend = get_htf_trend_latest(htf_buffer)
            log.info(f"HTF candle closed | {cfg.htf_timeframe} trend={trend}")
        except Exception as e:
            log.error(f"on_htf_candle error: {e}", exc_info=True)

    log.info(f"Listening for candles | {cfg.symbol} {cfg.timeframe} ...")

    if cfg.htf_enabled:
        await start_multi_kline_socket(
            client=client,
            symbol=cfg.symbol,
            handlers={
                cfg.timeframe: on_candle,
                cfg.htf_timeframe: on_htf_candle,
            },
            logger=log,
        )
    else:
        await start_kline_socket(client, cfg.symbol, cfg.timeframe, on_candle, logger=log)


if __name__ == "__main__":
    asyncio.run(main())
