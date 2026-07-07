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
LOCK_FILE_TEMPLATE = "bot.lock.{symbol}"


def _lock_file(symbol: str) -> str:
    return os.path.join(os.path.dirname(__file__) or ".", LOCK_FILE_TEMPLATE.replace("{symbol}", symbol.lower()))


def _acquire_lock(symbol: str) -> bool:
    lock_path = _lock_file(symbol)
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r") as f:
                pid = int(f.read().strip())
            # Проверяем, мертав ли процесс
            if pid > 0:
                try:
                    import subprocess
                    result = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {pid}"],
                        capture_output=True, text=True, timeout=5
                    )
                    if f",{pid}," in result.stdout or f" {pid} " in result.stdout:
                        return False
                except Exception:
                    pass
        except Exception:
            pass
        # Lock-файл битый или процесс мертв - удаляем
        try:
            os.remove(lock_path)
        except:
            pass
    # Создаём lock
    try:
        with open(lock_path, "w") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return False


def _release_lock(symbol: str) -> None:
    lock_path = _lock_file(symbol)
    try:
        os.remove(lock_path)
    except:
        pass


async def _sync_position_on_start(
    cfg, client: AsyncClient, tracker: PositionTracker,
    order_mgr: OrderManager, log
) -> None:
    if cfg.mode != "live":
        return

    try:
        positions = await client.futures_position_information(symbol=cfg.symbol)
    except Exception as e:
        log.error(f"[SYNC] Failed to fetch positions: {e}")
        return

    exchange_qty = 0.0
    for p in positions:
        amt = float(p.get("positionAmt", 0))
        if abs(amt) > 0:
            exchange_qty = abs(amt)

    if exchange_qty < 0.000001:
        if tracker.load_state():
            log.warning(f"[SYNC] Exchange shows no position but state has open position — clearing state")
            tracker.position = None
            tracker._clear_state()
        log.info(f"[SYNC] No open position found for {cfg.symbol}")
        return

    if tracker.load_state():
        pos = tracker.position
        if pos:
            try:
                real_qty = await order_mgr._get_real_position_qty(pos.direction)
                if real_qty < 0.000001:
                    log.warning(f"[SYNC] Exchange shows no position but state has open position — position closed externally (TP/SL), clearing state")
                    tracker.position = None
                    tracker._clear_state()
                    return
                if real_qty < pos.remaining_qty * 0.5:
                    log.warning(
                        f"[SYNC] Partial external close detected. "
                        f"Tracker qty={pos.remaining_qty:.6f} vs Exchange qty={real_qty:.6f}. "
                        f"Adjusting state and setting tp1_hit=True."
                    )
                    pos.remaining_qty = real_qty
                    pos.tp1_hit = True
                    pos.sl_price = pos.entry_price
                    tracker._save_state()
                    await _replace_tp_sl(order_mgr, pos, log)
                    return
                notional = real_qty * pos.entry_price
                if notional < 1.0:
                    log.warning(f"[SYNC] Dust position detected (qty={real_qty}, notional=${notional:.4f}), closing")
                    await order_mgr.close_dust(pos.direction)
                    tracker.position = None
                    tracker._clear_state()
                    return
            except Exception as e:
                log.warning(f"[SYNC] Could not verify position on exchange: {e}")
        await _replace_tp_sl(order_mgr, pos, log)
        return

    entry_price = 0.0
    direction = "LONG"
    for p in positions:
        amt = float(p.get("positionAmt", 0))
        if abs(amt) > 0:
            direction = "LONG" if amt > 0 else "SHORT"
            entry_price = float(p.get("entryPrice", 0))
            break

    if entry_price == 0:
        log.warning(f"[SYNC] Position found but entryPrice=0, skipping")
        return

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
        total_qty=exchange_qty,
        remaining_qty=exchange_qty,
    )

    import datetime
    mock_signal = Signal(
        direction=direction,
        entry_price=entry_price,
        sl_price=round(sl_price, 4),
        tp1_price=round(tp1_price, 4),
        tp2_price=round(tp2_price, 4),
        ema_fast=0, ema_slow=0, volume=0, volume_ma=0,
        timestamp=datetime.datetime.utcnow(),
    )
    await tracker._report_open(mock_signal, exchange_qty)
    tracker._save_state()

    log.info(
        f"[SYNC] Restored from exchange | {direction} {cfg.symbol} "
        f"qty={exchange_qty} entry={entry_price} "
        f"SL={sl_price:.4f} TP1={tp1_price:.4f} TP2={tp2_price:.4f} "
        f"(levels recalculated from config)"
    )

    await _replace_tp_sl(order_mgr, tracker.position, log)


async def _replace_tp_sl(order_mgr: OrderManager, pos, log) -> None:
    if not pos or pos.remaining_qty < 0.000001:
        log.warning(f"[SYNC] No position to replace TP/SL (remaining_qty={pos.remaining_qty if pos else 0})")
        return
    try:
        await order_mgr.cancel_all_tp_sl(pos.direction)
        import asyncio as _asyncio
        await _asyncio.sleep(1.5)
        log.info(f"[SYNC] Placing orders | sl_price={pos.sl_price} tp1_price={pos.tp1_price} tp2_price={pos.tp2_price} remaining_qty={pos.remaining_qty}")
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

    if not _acquire_lock(cfg.symbol):
        log.error(f"Another bot instance already running for {cfg.symbol} — exiting")
        sys.exit(1)

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
    log.debug(f"events logger: {events.name} handlers={len(events.handlers)}")

    try:
        if cfg.mode == "backtest":
            await run_backtest(cfg, client, log)
            return

        await _run_live_or_paper(cfg, client, log, reporter, recovery, shutdown_event, events)

    finally:
        _release_lock(cfg.symbol)
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
    order_mgr = OrderManager(cfg, log, client=client if cfg.mode == "live" else None)
    tracker   = PositionTracker(cfg, log, reporter=reporter, order_mgr=order_mgr if cfg.mode == "live" else None)
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
            log.debug(f"on_candle #{candle_count[0]} price={current_price}")

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
                # Синхронизируем unrealized PnL с биржей для открытых позиций
                if cfg.mode == "live" and tracker.has_open_position():
                    await tracker.sync_unrealized_pnl()

            if tracker.has_open_position():
                # Проверяем реальный объём позиции на бирже (раз в 12 свечей ~ 1 минута)
                pos = tracker.position
                if pos and candle_count[0] % 12 == 0 and cfg.mode == "live":
                    try:
                        real_qty = await order_mgr._get_real_position_qty(pos.direction)
                        if real_qty < 0:
                            # API error — skip sync, don't treat as closed
                            events.debug(f"POSITION_SYNC | API error (qty={real_qty}), skipping")
                        elif real_qty < pos.remaining_qty * 0.5:
                            events.warning(
                                f"POSITION_SYNC | tracker_qty={pos.remaining_qty} "
                                f"exchange_qty={real_qty:.6f} — position closed externally"
                            )
                            if real_qty < 0.001:
                                # Полностью закрыта на бирже без нашего участия.
                                # Определяем причину направление-зависимо (для SHORT
                                # прибыль — это падение цены, не рост) и применяем
                                # через уже проверенный apply_hit_async путь, чтобы
                                # получить правильный знак PnL, учёт предыдущего
                                # частичного TP1 и корректную синхронизацию с биржей —
                                # вместо пересчёта PnL заново здесь.
                                price_moved_favorably = (
                                    current_price > pos.entry_price if pos.direction == "LONG"
                                    else current_price < pos.entry_price
                                )
                                hit_type = "TP2" if price_moved_favorably else "SL"
                                events.warning(
                                    f"POSITION_SYNC | Full close detected as {hit_type} at price={current_price}"
                                )
                                pnl = await tracker.apply_hit_async(hit_type, current_price)
                                # Отменяем оставшиеся ордера на бирже
                                await order_mgr.cancel_all_tp_sl(pos.direction)
                                if pos.is_recovery:
                                    await recovery.report(pnl=pnl, chain_id=pos.recovery_chain_id)
                                elif pnl < 0:
                                    await recovery.report(pnl=pnl)
                            else:
                                # Закрыта частично (между 0% и 50% от того, что бот
                                # считал открытым) — скорректируем remaining_qty в
                                # трекере, не закрывая сделку, и продолжим обычное
                                # наблюдение на следующих свечах.
                                events.warning(
                                    f"POSITION_SYNC | Partial external close — "
                                    f"adjusting tracked qty {pos.remaining_qty} -> {real_qty:.6f}"
                                )
                                pos.remaining_qty = real_qty
                                tracker._save_state()
                            return
                    except Exception as e:
                        events.warning(f"POSITION_SYNC | Error: {e}")

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
                        # Отменяем оставшиеся ордера (SL если остался)
                        await order_mgr.cancel_all_tp_sl(pos.direction)
                        if pos.is_recovery:
                            await recovery.report(pnl=pnl, chain_id=pos.recovery_chain_id)
                    else:
                        # SL: биржа закрыла позицию
                        events.info(f"SL_HIT | price={current_price} qty={pos.remaining_qty} tp1_hit={pos.tp1_hit}")
                        pnl = await tracker.apply_hit_async(hit, current_price)
                        events.info(f"SL_APPLY | pnl={pnl}")
                        # Отменяем оставшиеся ордера (TP1/TP2 если остались)
                        await order_mgr.cancel_all_tp_sl(pos.direction)
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
