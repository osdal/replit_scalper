import asyncio
import logging
import os
import sys
import signal
from typing import Optional

import pandas as pd
from binance import AsyncClient
from dotenv import load_dotenv

from config import load_config
from logger import get_logger, get_events_logger
from market_data import get_recent_klines, start_kline_polling
from strategy import calculate_indicators, calculate_htf_indicators, get_signal, get_htf_trend_latest, Signal
from signal_handler import SignalHandler
from order_manager import OrderManager, calc_recovery_quantity
from position_tracker import PositionTracker, Position
from backtester import run_backtest
from db_reporter import DbReporter
from recovery_client import RecoveryClient, readRecoveryConfig

load_dotenv()

HEARTBEAT_CANDLES = 3

shutdown_event: Optional[asyncio.Event] = None


async def _sync_position_on_start(
    cfg, client: AsyncClient, tracker: PositionTracker,
    order_mgr: OrderManager, log
) -> None:
    if cfg.mode != "live":
        return

    if tracker.load_state():
        try:
            positions = await client.futures_position_information(symbol=cfg.symbol)
            exchange_has_position = any(
                abs(float(p.get("positionAmt", 0))) > 0 for p in positions
            )
            if not exchange_has_position:
                log.warning(
                    f"[SYNC] State file has position but exchange shows none — "
                    f"clearing state file"
                )
                tracker.position = None
                tracker._clear_state()
                return
        except Exception as e:
            log.warning(f"[SYNC] Could not verify position on exchange: {e}")

        pos = tracker.position
        await _replace_tp_sl(order_mgr, pos, log)
        return

    try:
        positions = await client.futures_position_information(symbol=cfg.symbol)
    except Exception as e:
        log.error(f"[SYNC] Failed to fetch positions: {e}")
        return

    for p in positions:
        amt = float(p.get("positionAmt", 0))
        if amt == 0:
            continue

        direction = "LONG" if amt > 0 else "SHORT"
        qty = abs(amt)
        entry_price = float(p.get("entryPrice", 0))

        if entry_price == 0:
            log.warning(f"[SYNC] Position found but entryPrice=0, skipping")
            continue

        sl_dist  = entry_price * cfg.sl_pct  / 100
        tp1_dist = entry_price * cfg.tp1_pct / 100
        tp2_dist = entry_price * cfg.tp2_pct / 100

        if direction == "LONG":
            sl_price  = entry_price - sl_dist
            tp1_price = entry_price + tp1_dist
            tp2_price = entry_price + tp2_dist
        else:
            sl_price  = entry_price + sl_dist
            tp1_price = entry_price - tp1_dist
            tp2_price = entry_price - tp2_dist

        tracker.position = Position(
            direction=direction,
            entry_price=entry_price,
            sl_price=round(sl_price, 4),
            tp1_price=round(tp1_price, 4),
            tp2_price=round(tp2_price, 4),
            total_qty=qty,
            remaining_qty=qty,
        )
        tracker._save_state()

        log.info(
            f"[SYNC] Restored from exchange | {direction} {cfg.symbol} "
            f"qty={qty} entry={entry_price} "
            f"SL={sl_price:.4f} TP1={tp1_price:.4f} TP2={tp2_price:.4f} "
            f"(levels recalculated from config)"
        )

        await _replace_tp_sl(order_mgr, tracker.position, log)
        return

    log.info(f"[SYNC] No open position found for {cfg.symbol}")


async def _replace_tp_sl(order_mgr: OrderManager, pos, log) -> None:
    try:
        await order_mgr.cancel_all_tp_sl(pos.direction)
        import asyncio as _asyncio
        await _asyncio.sleep(1.5)
        await order_mgr._place_all_orders(
            direction=pos.direction,
            total_qty=pos.remaining_qty,
            sl_price=pos.sl_price,
            tp1_price=pos.tp1_price,
            tp2_price=pos.tp2_price,
        )
        log.info(f"[SYNC] TP/SL orders replaced on exchange")
    except Exception as e:
        log.error(f"[SYNC] Failed to replace TP/SL orders: {e}", exc_info=True)


def _setup_signal_handlers(log):
    def signal_handler(signum, frame):
        sig_name = signal.Signals(signum).name
        log.info(f"Received signal {sig_name} ({signum}), initiating graceful shutdown...")
        if shutdown_event:
            shutdown_event.set()
    
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)


async def main():
    global shutdown_event
    
    config_path = sys.argv[1] if len(sys.argv) > 1 else "config.yaml"
    cfg = load_config(config_path)
    log = get_logger(log_file=cfg.log_file, mode=cfg.mode, symbol=cfg.symbol)

    log.info(f"Bot starting | mode={cfg.mode} symbol={cfg.symbol} tf={cfg.timeframe}")
    log.info(
        f"Config | leverage={cfg.leverage}x risk={cfg.risk_pct}% "
        f"SL={cfg.sl_pct}% TP1={cfg.tp1_pct}% TP2={cfg.tp2_pct}% auto={cfg.auto_mode}"
    )
    if cfg.htf_enabled:
        log.info(f"HTF filter | {cfg.htf_timeframe} EMA{cfg.htf_ema_fast}/{cfg.htf_ema_slow}")

    api_key    = os.getenv("BINANCE_API_KEY", "")
    api_secret = os.getenv("BINANCE_API_SECRET", "")

    if cfg.mode == "live" and (not api_key or not api_secret):
        log.error("LIVE mode requires BINANCE_API_KEY and BINANCE_API_SECRET in .env")
        sys.exit(1)

    client = await AsyncClient.create(
        api_key=api_key or None,
        api_secret=api_secret or None,
    )

    reporter = DbReporter(symbol=cfg.symbol, logger=log)
    recovery = RecoveryClient(symbol=cfg.symbol, logger=log)
    
    shutdown_event = asyncio.Event()
    _setup_signal_handlers(log)

    events = get_events_logger(cfg.symbol)
    print(f"[DEBUG] events logger: {events.name} handlers={len(events.handlers)}", flush=True)

    try:
        if cfg.mode == "backtest":
            await run_backtest(cfg, client, log)
            return

        await _run_live_or_paper(cfg, client, log, reporter, recovery, shutdown_event, events)

    finally:
        await reporter.report_stopped()
        await reporter.close()
        await recovery.close()
        await client.close_connection()
        log.info("Bot stopped")


async def _run_live_or_paper(
    cfg, client: AsyncClient, log,
    reporter: DbReporter, recovery: RecoveryClient,
    shutdown_event: asyncio.Event,
    events: logging.Logger,
):
    tracker   = PositionTracker(cfg, log, reporter=reporter)
    order_mgr = OrderManager(cfg, log, client=client if cfg.mode == "live" else None)
    handler   = SignalHandler(cfg, log)

    await _sync_position_on_start(cfg, client, tracker, order_mgr, log)
    await reporter.report_heartbeat(0)

    df_buffer: pd.DataFrame = await get_recent_klines(
        client=client, symbol=cfg.symbol, interval=cfg.timeframe,
        limit=max(cfg.ema_slow * 3, 200),
    )
    df_buffer = calculate_indicators(df_buffer, cfg)
    log.info(f"Loaded {len(df_buffer)} candles for warm-up ({cfg.timeframe})")

    htf_buffer: pd.DataFrame = pd.DataFrame()
    if cfg.htf_enabled:
        htf_buffer = await get_recent_klines(
            client=client, symbol=cfg.symbol, interval=cfg.htf_timeframe,
            limit=max(cfg.htf_ema_slow * 3, 100),
        )
        htf_buffer = calculate_htf_indicators(htf_buffer, cfg)
        trend = get_htf_trend_latest(htf_buffer)
        log.info(f"Loaded {len(htf_buffer)} candles for HTF warm-up ({cfg.htf_timeframe}) | trend={trend}")

    candle_count = [0]

    async def on_candle(candle: pd.Series):
        nonlocal df_buffer
        _ = events  # capture events in closure
        try:
            new_row = pd.DataFrame([candle]).set_index("open_time")
            df_buffer = pd.concat([df_buffer, new_row]).tail(500)
            df_buffer = calculate_indicators(df_buffer, cfg)

            current_price = float(candle["close"])
            candle_count[0] += 1
            print(f"[DEBUG] on_candle #{candle_count[0]} price={current_price}", flush=True)

            await reporter.report_heartbeat(current_price)
            if tracker.has_open_position():
                pos = tracker.position
                await reporter.report_position({
                    "direction":    pos.direction,
                    "entry_price":  pos.entry_price,
                    "sl_price":     pos.sl_price,
                    "tp1_price":    pos.tp1_price,
                    "tp2_price":    pos.tp2_price,
                    "total_qty":    pos.total_qty,
                    "remaining_qty": pos.remaining_qty,
                    "tp1_hit":      pos.tp1_hit,
                    "realized_pnl": pos.realized_pnl,
                })
            else:
                await reporter.report_position(None)

            if candle_count[0] % HEARTBEAT_CANDLES == 0:
                htf_trend_now = get_htf_trend_latest(htf_buffer) if cfg.htf_enabled else "off"
                log.info(f"Heartbeat | candles={candle_count[0]} price={current_price:.2f} htf_trend={htf_trend_now}")

            if tracker.has_open_position():
                hit = tracker.check(current_price)
                if hit:
                    pos = tracker.position
                    if hit == "TP1" and not pos.is_recovery:
                        # TP1: биржа закрыла часть, бот только обновляет стейт и переносит SL
                        events.info(f"TP1_HIT | price={current_price} total_qty={pos.total_qty} remaining_qty={pos.remaining_qty} old_sl={pos.sl_price}")
                        pnl = await tracker.apply_hit_async(hit, current_price)
                        new_sl = tracker.position.sl_price if tracker.position else 'N/A'
                        events.info(f"TP1_APPLY | pnl={pnl} new_sl={new_sl} remaining_qty={tracker.position.remaining_qty if tracker.position else 0}")
                        await order_mgr.move_sl_to_breakeven(
                            pos.direction, pos.entry_price,
                            remaining_qty=tracker.position.remaining_qty if tracker.position else 0.0,
                            tp2_price=pos.tp2_price,
                        )
                    elif hit == "TP2":
                        # TP2: биржа закрыла остаток, бот фиксирует результат
                        events.info(f"TP2_HIT | price={current_price} qty={pos.remaining_qty}")
                        pnl = await tracker.apply_hit_async(hit, current_price)
                        events.info(f"TP2_APPLY | pnl={pnl}")
                        if pos.is_recovery:
                            await recovery.report(pnl=pnl, chain_id=pos.recovery_chain_id)
                    else:
                        # SL: биржа закрыла позицию
                        events.info(f"SL_HIT | price={current_price} qty={pos.remaining_qty} tp1_hit={pos.tp1_hit}")
                        pnl = await tracker.apply_hit_async(hit, current_price)
                        events.info(f"SL_APPLY | pnl={pnl}")
                        if pos.is_recovery:
                            await recovery.report(pnl=pnl, chain_id=pos.recovery_chain_id)
                        elif pnl < 0:
                            await recovery.report(pnl=pnl)
                return

            raw_signal = get_signal(df_buffer, cfg)
            if raw_signal is None:
                return

            if cfg.htf_enabled:
                htf_trend = get_htf_trend_latest(htf_buffer)
                if htf_trend is not None and raw_signal.direction != htf_trend:
                    log.info(f"Signal {raw_signal.direction} BLOCKED by HTF | htf_trend={htf_trend}")
                    return

            signal = raw_signal
            confirmed = await handler.confirm(signal)
            if not confirmed:
                return

            # Пробуем захватить свободный долг для recovery-режима
            claim = await recovery.claim()
            recovery_qty = None
            chain_id = None
            if claim.get("chainId") is not None:
                chain_id = claim["chainId"]
                debt = claim["debtAmount"]
                bonus = claim.get("bonusPct", 0.0)
                balance = await order_mgr.get_balance()
                # Читаем max_pct из recovery_config.yaml (0 = без ограничения)
                rec_cfg = readRecoveryConfig()
                max_pct = rec_cfg.get("recovery_max_pct", 50.0)
                recovery_qty = calc_recovery_quantity(
                    debt_amount=debt,
                    bonus_pct=bonus,
                    tp1_pct=cfg.tp1_pct,
                    entry_price=signal.entry_price,
                    balance=balance,
                    risk_pct=cfg.risk_pct,
                    sl_pct=cfg.sl_pct,
                    max_pct=max_pct if max_pct > 0 else None,
                )
                log.info(
                    f"[RECOVERY] Claimed chain #{chain_id} | debt={debt:.4f} "
                    f"bonus={bonus}% max_pct={max_pct}% recovery_qty={recovery_qty:.6f}"
                )

            result = await order_mgr.open_position(signal, recovery_qty=recovery_qty)
            if result is not None:
                entry_price, qty = result
                signal.entry_price = entry_price
                is_recovery = recovery_qty is not None
                await tracker.open_async(
                    signal, qty=qty,
                    is_recovery=is_recovery,
                    recovery_chain_id=chain_id,
                )
                events.info(f"POSITION_OPEN | {signal.direction} {cfg.symbol} entry={entry_price} qty={qty} is_recovery={is_recovery} chain_id={chain_id}")
            elif chain_id is not None:
                log.warning(f"[RECOVERY] Failed to open position for chain #{chain_id} — releasing")
                await recovery.release(chain_id=chain_id)

        except Exception as e:
            log.error(f"on_candle error: {e}", exc_info=True)

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

    handlers = {cfg.timeframe: on_candle}
    if cfg.htf_enabled:
        handlers[cfg.htf_timeframe] = on_htf_candle

    await start_kline_polling(
        client=client, symbol=cfg.symbol, handlers=handlers,
        logger=log, poll_seconds=10, shutdown_event=shutdown_event,
    )


if __name__ == "__main__":
    asyncio.run(main())
