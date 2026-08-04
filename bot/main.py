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
from order_manager import OrderManager
from position_tracker import PositionTracker, Position
from backtester import run_backtest
from db_reporter import DbReporter
from recovery_client import RecoveryClient
from notifier import Notifier

load_dotenv()

HEARTBEAT_CANDLES = 3
LOCK_FILE_TEMPLATE = "bot.lock.{symbol}"

# Глобальные переменные для отслеживания recovery-состояния
_recovery_state = {}  # {symbol: {"chainId": int, "debtAmount": float, "is_recovery": bool}}

# Очередь симуляции исходов отклонённых сигналов.
# Каждый элемент: {"trade_id": int, "direction": str, "entry": float, "sl": float,
#                  "tp1": float, "candles": int} — закрыт по SL/TP1 либо истёк по времени.
_rejected_sims = []
_REJECTED_SIM_MAX_CANDLES = 24  # максимум свечей ждём результат (24×5м = 2ч)


async def _simulate_rejected_outcome(current_price, reporter, log):
    """Продвигает симуляцию исходов отклонённых сигналов: если цена дошла до
    SL или TP1 фиксируем результат как exit_reason/exit_price/pnl (симулируемое,
    позиция не открывалась). Истёкшие по времени отметки убираем без результата."""
    if not _rejected_sims or reporter is None:
        return
    kept = []
    for sim in _rejected_sims:
        sim["candles"] += 1
        direction = sim.get("direction", "LONG")
        entry = sim.get("entry")
        sl = sim.get("sl")
        tp1 = sim.get("tp1")
        tid = sim.get("trade_id")

        hit = None
        hit_price = None
        if direction == "LONG":
            if current_price <= sl:
                hit, hit_price = "SL", current_price
            elif current_price >= tp1:
                hit, hit_price = "TP1", current_price
        else:
            if current_price >= sl:
                hit, hit_price = "SL", current_price
            elif current_price <= tp1:
                hit, hit_price = "TP1", current_price

        if hit and entry:
            # Симулируемый PnL по цене (без плеча/количества, qty=0 → это 0; покажем % отдельно)
            pnl = 0.0  # фактической позиции не было; результат - только факт исхода
            import datetime as _dt
            try:
                await reporter.patch_trade(tid, {
                    "exit_price": hit_price,
                    "exit_reason": hit,
                    "pnl": 0.0,
                    "exit_time": _dt.datetime.utcnow().isoformat(),
                })
                log.info(f"[RISK_SIM] Rejected {sim.get('symbol','?')} would have HIT {hit} @ {hit_price:.4f} (trade #{tid})")
            except Exception as e:
                log.debug(f"[RISK_SIM] finalize error: {e}")
            continue  # удаляем из очереди

        if sim["candles"] >= _REJECTED_SIM_MAX_CANDLES:
            log.debug(f"[RISK_SIM] Rejected trade #{tid} expired with no TP/SL")
            continue  # удаляем без результата

        kept.append(sim)
    _rejected_sims[:] = kept


def _lock_file(symbol: str) -> str:
    return os.path.join(os.path.dirname(__file__) or ".", LOCK_FILE_TEMPLATE.replace("{symbol}", symbol.lower()))


def _process_is_bot(pid: int, symbol: str) -> bool:
    """Return True only if PID is a live Python process running this bot's main.py."""
    try:
        os.kill(pid, 0)
    except (OSError, SystemError):
        # On Windows os.kill(pid, 0) may fail even for live processes (WinError 87),
        # so we still proceed to inspect the command line and only decide there.
        pass

    config_hint = f"config_{symbol.replace('USDT', '').lower()}.yaml"
    try:
        import subprocess
        import platform
        if platform.system() == "Windows":
            # Use wmic; fall back to Get-CimInstance (Windows 10/11) for the command line
            try:
                result = subprocess.run(
                    ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/value"],
                    capture_output=True, text=True, timeout=5,
                )
                out = result.stdout
            except Exception:
                try:
                    result = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         f"Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" | Select-Object -ExpandProperty CommandLine"],
                        capture_output=True, text=True, timeout=8,
                    )
                    out = result.stdout
                except Exception:
                    result = subprocess.run(
                        ["tasklist", "/V", "/FI", f"PID eq {pid}"],
                        capture_output=True, text=True, timeout=5,
                    )
                    out = result.stdout
        else:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "command="],
                capture_output=True, text=True, timeout=5,
            )
            out = result.stdout
        # If no process matched (empty output), it's not a live bot.
        if not out or (platform.system() != "Windows" and result.returncode != 0):
            return False
        return ("main.py" in out) and (config_hint in out)
    except Exception:
        # If we can't inspect the command line, treat the process as not a bot
        # (safer: allow re-acquiring the lock rather than blocking forever.)
        return False


def _acquire_lock(symbol: str) -> bool:
    lock_path = _lock_file(symbol)
    if os.path.exists(lock_path):
        try:
            with open(lock_path, "r") as f:
                pid = int(f.read().strip())
            if pid > 0 and _process_is_bot(pid, symbol):
                # A live bot is already running — don't allow a second instance
                return False
        except Exception:
            pass
        # Lock-файл битый или процесс не является работающим ботом - удаляем
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
    order_mgr: OrderManager, log, recovery=None, notifier=None,
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
            sync_pos = tracker.position
            # Close stale DB trades with real PnL from Binance
            try:
                import requests as sync_requests
                import datetime
                api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5000/api")
                trades_resp = sync_requests.get(f"{api_url}/trades?symbol={cfg.symbol}&limit=10", timeout=5).json()
                for trade in (trades_resp.get("trades") or []):
                    if trade.get("is_open"):
                        trade_id = trade["id"]
                        entry_time_str = trade.get("entry_time", "")
                        exit_ms = int(__import__("time").time() * 1000)
                        entry_ms = 0
                        try:
                            if entry_time_str:
                                entry_ms = int(datetime.datetime.fromisoformat(entry_time_str[:19].replace("T", " ").replace("Z", "")).timestamp() * 1000)
                        except Exception:
                            pass
                        real_pnl = None
                        if entry_ms > 0 and order_mgr:
                            real_pnl = await order_mgr.get_realized_pnl(cfg.symbol, entry_ms, exit_ms)
                        pnl_val = real_pnl if (real_pnl is not None and abs(real_pnl) > 0.0001) else 0.0
                        sync_requests.patch(f"{api_url}/trades/{trade_id}", json={
                            "is_open": False,
                            "exit_reason": "SL",
                            "pnl": round(pnl_val, 4),
                            "exit_time": datetime.datetime.utcnow().isoformat(),
                        }, timeout=5)
                        log.info(f"[SYNC] Closed stale trade #{trade_id} for {cfg.symbol} | pnl={pnl_val:.4f}")
                        if pnl_val < 0 and recovery:
                            await recovery.report(pnl=pnl_val)
                            log.info(f"[SYNC] Reported recovery from stale trade #{trade_id} | pnl={pnl_val:.4f}")
                            # Освобождаем захваченную recovery-цепочку, если позиция её держала,
                            # чтобы она не осталась навсегда в статусе locked.
                            if sync_pos and getattr(sync_pos, "recovery_chain_id", None):
                                await recovery.release(chain_id=sync_pos.recovery_chain_id)
                                log.info(f"[SYNC] Released locked recovery chain #{sync_pos.recovery_chain_id} for {cfg.symbol}")
            except Exception as e:
                log.debug(f"[SYNC] Cleanup error: {e}")
            tracker.position = None
            tracker._clear_state()
            # Биржа показывает, что позиции нет — любые locked-цепочки этого
            # символа зависли (бот упал между claim и открытием, или позиция
            # была закрыта вне бота без отчёта). Освобождаем все такие цепочки.
            if recovery:
                await recovery.release_all_for_symbol()
        else:
            # No tracker state either — check for stale DB trades, fetch real PnL
            # А также освобождаем "зависшие" locked-цепочки: бот мог упасть
            # между claim и открытием позиции, оставив цепочку locked навсегда.
            if recovery:
                await recovery.release_all_for_symbol()
            try:
                import requests as sync_requests
                import datetime
                api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5000/api")
                trades_resp = sync_requests.get(f"{api_url}/trades?symbol={cfg.symbol}&limit=10", timeout=5).json()
                for trade in (trades_resp.get("trades") or []):
                    if trade.get("is_open"):
                        trade_id = trade["id"]
                        entry_time_str = trade.get("entry_time", "")
                        exit_ms = int(__import__("time").time() * 1000)
                        entry_ms = 0
                        try:
                            if entry_time_str:
                                entry_ms = int(datetime.datetime.fromisoformat(entry_time_str[:19].replace("T", " ").replace("Z", "")).timestamp() * 1000)
                        except Exception:
                            pass
                        real_pnl = None
                        if entry_ms > 0 and order_mgr:
                            real_pnl = await order_mgr.get_realized_pnl(cfg.symbol, entry_ms, exit_ms)
                        pnl_val = real_pnl if (real_pnl is not None and abs(real_pnl) > 0.0001) else 0.0
                        sync_requests.patch(f"{api_url}/trades/{trade_id}", json={
                            "is_open": False,
                            "pnl": round(pnl_val, 4),
                            "exit_reason": "SL",
                            "exit_time": datetime.datetime.utcnow().isoformat(),
                        }, timeout=5)
                        log.info(f"[SYNC] Closed stale trade #{trade_id} for {cfg.symbol} | pnl={pnl_val:.4f}")
                        if pnl_val < 0 and recovery:
                            await recovery.report(pnl=pnl_val)
            except Exception as e:
                log.debug(f"[SYNC] Stale trade cleanup error: {e}")
        log.info(f"[SYNC] No open position found for {cfg.symbol}")
        return

    if tracker.load_state():
        pos = tracker.position
        if pos:
            try:
                real_qty = await order_mgr._get_real_position_qty(pos.direction)
                if real_qty < 0.000001:
                    log.warning(f"[SYNC] Exchange shows no position but state has open position — position closed externally (TP/SL), clearing state")
                    # Fetch real PnL and close DB trade
                    try:
                        import requests as s2
                        import datetime
                        api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5000/api")
                        trades_resp = s2.get(f"{api_url}/trades?symbol={cfg.symbol}&limit=10", timeout=5).json()
                        for trade in (trades_resp.get("trades") or []):
                            if trade.get("is_open") and pos.entry_timestamp:
                                entry_ms = 0
                                try:
                                    if isinstance(pos.entry_timestamp, str):
                                        entry_ms = int(datetime.datetime.fromisoformat(pos.entry_timestamp).timestamp() * 1000)
                                    else:
                                        entry_ms = int(pos.entry_timestamp.timestamp() * 1000)
                                except Exception:
                                    pass
                                exit_ms = int(__import__("time").time() * 1000)
                                real_pnl = None
                                if entry_ms > 0:
                                    real_pnl = await order_mgr.get_realized_pnl(cfg.symbol, entry_ms, exit_ms)
                                pnl_val = real_pnl if (real_pnl is not None and abs(real_pnl) > 0.0001) else 0.0
                                s2.patch(f"{api_url}/trades/{trade['id']}", json={
                                    "is_open": False, "exit_reason": "SL",
                                    "pnl": round(pnl_val, 4),
                                    "exit_time": datetime.datetime.utcnow().isoformat(),
                                }, timeout=5)
                                log.info(f"[SYNC] Closed stale trade #{trade['id']} after external close | pnl={pnl_val:.4f}")
                                if pnl_val < 0 and recovery:
                                    await recovery.report(pnl=pnl_val)
                                    # Освобождаем захваченную recovery-цепочку, если эта позиция её держала.
                                    if pos and getattr(pos, "recovery_chain_id", None):
                                        await recovery.release(chain_id=pos.recovery_chain_id)
                                        log.info(f"[SYNC] Released locked recovery chain #{pos.recovery_chain_id} after external close for {cfg.symbol}")
                    except Exception:
                        pass
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
    # Check for locked recovery chain to preserve recovery context
    if recovery and cfg.mode == "live":
        import requests
        try:
            api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5000/api")
            chains = requests.get(f"{api_url}/recovery/chains", timeout=5).json()
            for ch in chains:
                if ch.get("locked_by") == cfg.symbol and ch.get("status") == "locked":
                    tracker.position.is_recovery = True
                    tracker.position.recovery_chain_id = ch["id"]
                    log.info(f"[SYNC] Marked position as recovery | chain #{ch['id']}")
                    break
        except Exception:
            pass
    # Register open trade in DB so _trade_id is set for future close handling.
    # Если для этого символа уже есть открытая сделка в БД — ПЕРЕИСПОЛЬЗУЕМ её
    # (это та же позиция на бирже, восстановленная после рестарта), чтобы не
    # задваивать записи в дашборде. Новую запись создаём только если открытой
    # сделки в БД нет.
    import requests
    try:
        api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5000/api")
        existing = requests.get(f"{api_url}/trades?symbol={cfg.symbol}&limit=20", timeout=5).json()
        existing_open = None
        for old_trade in (existing.get("trades") or []):
            if old_trade.get("is_open"):
                existing_open = old_trade
                break
        if existing_open and existing_open.get("id"):
            # Переиспользуем существующую запись: обновляем цены/объём под биржу и
            # привязываем к трекеру, чтобы закрытие патилось в правильную запись.
            reuse_id = int(existing_open["id"])
            requests.patch(
                f"{api_url}/trades/{reuse_id}",
                json={"entry_price": entry_price, "qty": exchange_qty, "exit_reason": None},
                timeout=5,
            )
            tracker._trade_id = reuse_id
            log.info(f"[SYNC] Reusing existing open trade #{reuse_id} for {cfg.symbol} (no duplicate created)")
        else:
            # Открытой записи нет — создаём новую
            await tracker._report_open(mock_signal, exchange_qty)
    except Exception:
        pass
    tracker._save_state()

    log.info(
        f"[SYNC] Restored from exchange | {direction} {cfg.symbol} "
        f"qty={exchange_qty} entry={entry_price} "
        f"SL={sl_price:.4f} TP1={tp1_price:.4f} TP2={tp2_price:.4f} "
        f"(levels recalculated from config)"
    )

    closed_immediately = await _replace_tp_sl(order_mgr, tracker.position, log)
    if closed_immediately:
        # SL was already breached — position closed with market order.
        # Clear tracker state so no phantom position is monitored.
        tracker.position = None
        tracker._clear_state()


async def _replace_tp_sl(order_mgr: OrderManager, pos, log) -> bool:
    """Replace TP/SL orders on exchange. Returns True if position was
    closed immediately due to SL being breached at restore time."""
    if not pos or pos.remaining_qty < 0.000001:
        log.warning(f"[SYNC] No position to replace TP/SL (remaining_qty={pos.remaining_qty if pos else 0})")
        return False
    try:
        # Check if market has already breached the SL level.
        # If so, the STOP_MARKET won't trigger until price returns yet the
        # tracker would consider it hit — leaving an orphan on the exchange.
        # Close the position immediately with a real market order instead.
        import asyncio as _asyncio
        try:
            ticker = await order_mgr.client.futures_symbol_ticker(symbol=order_mgr.cfg.symbol)
            current_price = float(ticker.get("price", 0))
        except Exception:
            current_price = 0.0
        if current_price > 0:
            breached = (pos.direction == "LONG" and current_price <= pos.sl_price) or \
                       (pos.direction == "SHORT" and current_price >= pos.sl_price)
            if breached:
                log.warning(
                    f"[SYNC] SL already breached on restore | {pos.direction} "
                    f"current={current_price:.4f} sl={pos.sl_price:.4f} — "
                    f"closing position with market order"
                )
                side = "SELL" if pos.direction == "LONG" else "BUY"
                await order_mgr.client.futures_create_order(
                    symbol=order_mgr.cfg.symbol,
                    side=side,
                    type="MARKET",
                    quantity=pos.remaining_qty,
                    reduceOnly=True,
                )
                return True
        await order_mgr.cancel_all_tp_sl(pos.direction)
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
    return False


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

        await _run_live_or_paper(cfg, client, log, reporter, recovery, shutdown_event, events, Notifier())

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
    notifier: Notifier,
):
    order_mgr = OrderManager(cfg, log, client=client if cfg.mode == "live" else None)
    tracker   = PositionTracker(cfg, log, reporter=reporter, order_mgr=order_mgr if cfg.mode == "live" else None, notifier=notifier)
    handler   = SignalHandler(cfg, log)

    await _sync_position_on_start(cfg, client, tracker, order_mgr, log, recovery, notifier)

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
            log.debug(f"Close price raw: {candle['close']}, current_price={current_price}")
            candle_time_ms = int(candle.name.timestamp() * 1000)
            candle_count[0] += 1
            log.debug(f"on_candle #{candle_count[0]} price={current_price}")

            await reporter.report_heartbeat(current_price)
            await _simulate_rejected_outcome(current_price, reporter, log)
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

            # Check for dust positions and stale orders on exchange every 12 candles
            if candle_count[0] % 12 == 0 and cfg.mode == "live":
                try:
                    all_positions = await order_mgr.client.futures_position_information()
                    syms_with_pos = set()
                    for p in all_positions:
                        amt = float(p.get("positionAmt", "0") or 0)
                        sym = p.get("symbol", "")
                        if abs(amt) < 0.001:
                            continue
                        syms_with_pos.add(sym)
                        ticker = await order_mgr.client.futures_symbol_ticker(symbol=sym)
                        price = float(ticker.get("price", 0))
                        notional = abs(amt) * price
                        if notional < 1.0:
                            direction = "LONG" if amt > 0 else "SHORT"
                            side = "SELL" if amt > 0 else "BUY"
                            await order_mgr.client.futures_create_order(
                                symbol=sym, side=side, type="MARKET",
                                quantity=abs(amt), reduceOnly=True,
                            )
                            log.info(f"[DUST] Closed dust on {sym} | {direction} qty={abs(amt)} notional=${notional:.4f}")
                    
                    # Cancel stale orders on symbols with no position and no tracker position
                    bot_sym = cfg.symbol
                    if bot_sym not in syms_with_pos and not tracker.has_open_position():
                        try:
                            open_orders = await order_mgr.client.futures_get_open_orders(symbol=bot_sym)
                            if open_orders:
                                await order_mgr.client.futures_cancel_all_open_orders(symbol=bot_sym)
                                log.info(f"[DUST] Canceled {len(open_orders)} stale orders on {bot_sym} (no position)")
                        except Exception:
                            pass
                except Exception as e:
                    log.debug(f"[DUST] Check error: {e}")

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
                                pnl = await tracker.apply_hit_async(hit_type, current_price, candle_time_ms)
                                closed_qty = pos.remaining_qty
                                if hit_type == "TP2":
                                    notifier.send_event("tp2_hit", {"symbol": cfg.symbol, "direction": pos.direction, "entry_price": pos.entry_price, "exit_price": current_price, "pnl": pnl})
                                elif hit_type == "SL":
                                    notifier.send_event("sl_hit", {"symbol": cfg.symbol, "direction": pos.direction, "entry_price": pos.entry_price, "exit_price": current_price, "pnl": pnl})
                                # Отменяем оставшиеся ордера на бирже
                                await order_mgr.cancel_all_tp_sl(pos.direction)
                                if pos.is_recovery:
                                    await recovery.release(chain_id=pos.recovery_chain_id)
                                    await recovery.report(pnl=pnl)
                                    log.info(f"[RECOVERY] External close on recovery | released chain #{pos.recovery_chain_id}, new chain for loss={pnl:.4f}")
                                elif pnl < 0:
                                    await recovery.report(pnl=pnl)
                                await recovery.report_result(pnl)
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
                        pnl = await tracker.apply_hit_async(hit, current_price, candle_time_ms)
                        new_sl = tracker.position.sl_price if tracker.position else 'N/A'
                        events.info(f"TP1_APPLY | pnl={pnl} new_sl={new_sl} remaining_qty={tracker.position.remaining_qty if tracker.position else 0}")
                        # Notify TP1 hit
                        notifier.send_event("tp1_hit", {
                            "symbol": cfg.symbol,
                            "direction": pos.direction,
                            "entry_price": pos.entry_price,
                            "exit_price": current_price,
                            "pnl": pnl,
                        })
                        await order_mgr.move_sl_to_breakeven(
                            pos.direction, pos.entry_price,
                            remaining_qty=tracker.position.remaining_qty if tracker.position else 0.0,
                            tp2_price=pos.tp2_price,
                        )
                    elif hit == "TP1" and pos.is_recovery:
                        # Recovery TP1: закрытие 100% позиции, отмена ордеров, репорт
                        events.info(f"TP1_HIT_RECOVERY | price={current_price} qty={pos.remaining_qty}")
                        pnl = await tracker.apply_hit_async(hit, current_price, candle_time_ms)
                        events.info(f"TP1_APPLY_RECOVERY | pnl={pnl}")
                        await order_mgr.cancel_all_tp_sl(pos.direction)
                        real_qty = await order_mgr._get_real_position_qty(pos.direction)
                        if real_qty > 0 and real_qty < 0.001:
                            await order_mgr.close_dust(pos.direction)
                        await recovery.report(pnl=pnl, chain_id=pos.recovery_chain_id)
                        await recovery.report_result(pnl)
                    elif hit == "TP2":
                        # TP2: биржа закрыла остаток, бот фиксирует результат
                        events.info(f"TP2_HIT | price={current_price} qty={pos.remaining_qty}")
                        pnl = await tracker.apply_hit_async(hit, current_price, candle_time_ms)
                        events.info(f"TP2_APPLY | pnl={pnl}")
                        # Notify TP2 hit
                        notifier.send_event("tp2_hit", {
                            "symbol": cfg.symbol,
                            "direction": pos.direction,
                            "entry_price": pos.entry_price,
                            "exit_price": current_price,
                            "pnl": pnl,
                        })
                        # Отменяем оставшиеся ордера (SL если остался)
                        await order_mgr.cancel_all_tp_sl(pos.direction)
                        # Проверяем, не осталась ли пылевая позиция
                        real_qty = await order_mgr._get_real_position_qty(pos.direction)
                        if real_qty > 0 and real_qty < 0.001:
                            await order_mgr.close_dust(pos.direction)
                        if pos.is_recovery:
                            await recovery.report(pnl=pnl, chain_id=pos.recovery_chain_id)
                        elif pnl < 0:
                            await recovery.report(pnl=pnl)
                        await recovery.report_result(pnl)
                    else:
                        # SL: биржа закрыла позицию
                        events.info(f"SL_HIT | price={current_price} qty={pos.remaining_qty} tp1_hit={pos.tp1_hit}")
                        pnl = await tracker.apply_hit_async(hit, current_price, candle_time_ms)
                        events.info(f"SL_APPLY | pnl={pnl}")
                        # Notify SL hit
                        notifier.send_event("sl_hit", {
                            "symbol": cfg.symbol,
                            "direction": pos.direction,
                            "entry_price": pos.entry_price,
                            "exit_price": current_price,
                            "pnl": pnl,
                        })
                        # Отменяем оставшиеся ордера (TP1/TP2 если остались)
                        await order_mgr.cancel_all_tp_sl(pos.direction)
                        # Проверяем, не осталась ли пылевая позиция
                        real_qty = await order_mgr._get_real_position_qty(pos.direction)
                        if real_qty > 0 and real_qty < 0.001:
                            await order_mgr.close_dust(pos.direction)
                        if pos.is_recovery:
                            await recovery.release(chain_id=pos.recovery_chain_id)
                            await recovery.report(pnl=pnl)
                            log.info(f"[RECOVERY] SL on recovery | released chain #{pos.recovery_chain_id}, new chain for loss={pnl:.4f}")
                        elif pnl < 0:
                            await recovery.report(pnl=pnl)
                        await recovery.report_result(pnl)
                        return

            # Защита от открытия второй позиции по той же монете.
            # Если после обработки сигналов TP/SL позиция всё ещё отслеживается
            # как открытая (например, после частичного TP1, когда remaining_qty > 0),
            # НЕ открываем новую сделку — иначе на бирже (one-way mode) две сделки
            # сольются в одну, а в дашборде появится дубль.
            if tracker.has_open_position():
                log.debug(f"[GUARD] Position still open for {cfg.symbol} — skip new signal")
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
            signal_data = {
                "direction": signal.direction,
                "symbol": cfg.symbol,
                "entry_price": signal.entry_price,
                "sl_price": signal.sl_price,
                "tp1_price": signal.tp1_price,
                "tp2_price": signal.tp2_price,
                "ema_fast": signal.ema_fast,
                "ema_slow": signal.ema_slow,
                "volume": signal.volume,
                "volume_ma": signal.volume_ma,
                "leverage": cfg.leverage,
            }
            notifier.send_signal(signal_data)
            confirmed = await handler.confirm(signal)
            if not confirmed:
                return

            # Глобальный риск-контроль: лимит позиций + пауза после серии убытков.
            # Проверяем ДО claim, чтобы не захватывать recovery-цепочку впустую.
            if recovery:
                risk_check = await recovery.can_open()
                if not risk_check.get("allowed", True):
                    reason = risk_check.get("reason", "risk_block")
                    log.info(
                        f"[RISK] Skip signal for {cfg.symbol}: reason={reason} "
                        f"(positions={risk_check.get('positions_open')})"
                    )
                    if reporter is not None:
                        tid = await reporter.report_rejected(signal_data, f"risk:{reason}")
                        if tid:
                            _rejected_sims.append({
                                "trade_id": tid,
                                "symbol": cfg.symbol,
                                "direction": signal_data.get("direction"),
                                "entry": signal_data.get("entry_price"),
                                "sl": signal_data.get("sl_price"),
                                "tp1": signal_data.get("tp1_price"),
                                "candles": 0,
                            })
                    return

            # Пробуем захватить свободный долг для recovery-режима
            claim = await recovery.claim()
            recovery_target = None
            chain_id = None
            
            # Логируем полный ответ от сервера
            log.info(f"[RECOVERY] claim response: {claim}")

            # Потолок долга: сервер отказывает в выдаче recovery-долга, когда
            # суммарный free+locked долг >= max_free_debt_usd. В этом случае
            # НЕ открываем даже обычную позицию — пропускаем сигнал, чтобы
            # не наращивать риск дальше.
            if claim.get("reason") == "debt_limit":
                log.warning(
                    f"[RISK] Debt limit reached (freeDebt={claim.get('freeDebt')})"
                    f" — skipping signal for {cfg.symbol}"
                )
                if reporter is not None:
                    tid = await reporter.report_rejected(signal_data, "risk:debt_limit")
                    if tid:
                        _rejected_sims.append({
                            "trade_id": tid,
                            "symbol": cfg.symbol,
                            "direction": signal_data.get("direction"),
                            "entry": signal_data.get("entry_price"),
                            "sl": signal_data.get("sl_price"),
                            "tp1": signal_data.get("tp1_price"),
                            "candles": 0,
                        })
                return
            
            # Сохраняем состояние recovery в глобальной переменной
            _recovery_state[cfg.symbol] = {
                "chainId": claim.get("chainId"),
                "debtAmount": claim.get("debtAmount", 0.0),
                "is_recovery": claim.get("chainId") is not None,
            }
            
            if claim.get("chainId") is not None:
                chain_id = claim["chainId"]
                debt = claim["debtAmount"]
                bonus = claim.get("bonusPct", 0.0)
                recovery_target = debt * (1 + bonus / 100)
                log.info(
                    f"[RECOVERY] Claimed chain #{chain_id} | debt={debt:.4f} "
                    f"bonus={bonus}% target_profit={recovery_target:.4f} USDT"
                )
                # Перед открытием компенсатора проверяем, что на бирже нет
                # уже открытой позиции по этому символу. Если позиция уже есть
                # (допустим, бот только что не успел её закрыть, или была
                # внешняя сделка) — recovery-ордер наложится на неё и на бирже
                # (one-way mode) они сольются в одну позицию. В таком случае
                # компенсировать нельзя — отпускаем цепочку.
                if cfg.mode == "live" and order_mgr:
                    try:
                        existing_qty = await order_mgr._get_real_position_qty(signal.direction)
                        if existing_qty >= 0.000001:
                            log.warning(
                                f"[RECOVERY] Skip chain #{chain_id} — position already open "
                                f"on {cfg.symbol} {signal.direction} qty={existing_qty:.6f}. "
                                f"Releasing chain."
                            )
                            await recovery.release(chain_id=chain_id)
                            return
                    except Exception as e:
                        log.warning(f"[RECOVERY] Position check failed ({e}) — proceeding cautiously")

            result = await order_mgr.open_position(signal, recovery_target=recovery_target)
            if result is not None:
                entry_price, qty = result[0], result[1]
                signal.entry_price = entry_price
                is_recovery = recovery_target is not None
                if is_recovery and len(result) > 2:
                    signal.tp1_price = result[2]
                    signal.tp2_price = result[2]  # no TP2 for recovery
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

    async def periodic_position_check():
        """Проверка состояния позиции каждые 1 час (3600 секунд)."""
        while not shutdown_event.is_set():
            try:
                await asyncio.sleep(3600)  # 1 час
                if shutdown_event.is_set():
                    return
                    
                if tracker.has_open_position():
                    pos = tracker.position
                    if pos:
                        # Получаем актуальную информацию о позиции с биржи
                        exchange_pos = await order_mgr.get_position_info()
                        if exchange_pos:
                            # Сравниваем количества
                            local_qty = pos.remaining_qty
                            exchange_qty = exchange_pos.get("qty", 0)
                            
                            # Порог для сравнения (небольшие различия из-за округления допустимы)
                            diff_threshold = 0.000001
                            if abs(local_qty - exchange_qty) > diff_threshold:
                                log.warning(
                                    f"[SYNC_WARNING] Position mismatch | "
                                    f"local_qty={local_qty:.6f} exchange_qty={exchange_qty:.6f} "
                                    f"direction={pos.direction}"
                                )
                                # Обновляем состояние
                                pos.remaining_qty = exchange_qty
                                tracker._save_state()
                                
                                # Проверяем, не закрыта ли позиция полностью
                                if exchange_qty < diff_threshold:
                                    log.warning(
                                        f"[SYNC_WARNING] Position appears closed on exchange | "
                                        f"updating local state"
                                    )
                                    pos.closed = True
                                    tracker._clear_state()
                            else:
                                events.debug(
                                    f"[SYNC_CHECK] Position state OK | "
                                    f"qty={exchange_qty:.6f} direction={pos.direction}"
                                )
                        else:
                            log.warning("[SYNC_WARNING] Could not fetch position info")
            except Exception as e:
                log.warning(f"[SYNC_CHECK] Error during periodic check: {e}")

    # Запускаем периодическую проверку состояния позиции в фоне
    check_task = asyncio.create_task(periodic_position_check())

    log.info(f"Listening for candles | {cfg.symbol} {cfg.timeframe} ...")

    handlers = {cfg.timeframe: on_candle}
    if cfg.htf_enabled:
        handlers[cfg.htf_timeframe] = on_htf_candle

    await start_kline_polling(
        client=client, symbol=cfg.symbol, handlers=handlers,
        logger=log, poll_seconds=10, shutdown_event=shutdown_event,
    )
    
    # Останавливаем фоновую задачу проверки позиции
    if not check_task.done():
        check_task.cancel()
        try:
            await check_task
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    asyncio.run(main())
