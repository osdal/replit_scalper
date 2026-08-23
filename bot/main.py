import asyncio
import logging
import os
import sys
import signal
import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
from binance import AsyncClient
from dotenv import load_dotenv

from config import load_config
from logger import get_logger, get_events_logger
from market_data import get_recent_klines, start_kline_polling
from strategy import calculate_indicators, calculate_htf_indicators, get_all_signals, get_htf_trend_latest, Signal, _calc_atr_sl_tp
from preset_config import get_preset_config
from signal_handler import SignalHandler
from order_manager import OrderManager
from position_tracker import PositionTracker, Position
from backtester import run_backtest
from db_reporter import DbReporter
from recovery_client import RecoveryClient
from notifier import Notifier

_this_dir = os.path.dirname(os.path.abspath(__file__))
_load = load_dotenv(os.path.join(_this_dir, "..", ".env")) or load_dotenv(os.path.join(_this_dir, ".env"))
if not _load:
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
SIM_COMMISSION_PCT = 0.05  # симулируемая комиссия Taker (%) для отклонённых сделок


def _sim_commission(entry, exit_price, qty):
    """Комиссия для симуляции в USDT (0.05% на вход и выход)."""
    if not entry or not exit_price or not qty:
        return 0.0
    fee = SIM_COMMISSION_PCT / 100.0
    return (abs(entry) + abs(exit_price)) * abs(qty) * fee


async def _track_skipped_signal(reporter, signal, cfg, reason):
    """Фиксирует сигнал, пропущенный лимитами/фильтрами, как rejected trade с
    reject_reason 'skip:*' (не симулируется, только статистика). Не блокирует цикл."""
    # При серии убытков каждый сигнал отклоняется локальной защитой. Запись каждой
    # пропущенной сделки в БД порождает поток 'skip:loss_streak_*' (сотни строк
    # в минуту при убыточной серии) — засоряет дашборд/БД. Блокировка работает и без
    # записи: просто логируем факт пропуска, не создавая записи в БД.
    if reason and reason.startswith("skip:loss_streak"):
        logging.getLogger("main").debug(
            f"[LOSS_STREAK] skip recording for {getattr(signal, 'symbol', '')}: {reason}"
        )
        return
    if reporter is None or signal is None:
        return
    try:
        import asyncio as _asyncio
        payload = {
            "direction": getattr(signal, "direction", "LONG"),
            "entry_price": getattr(signal, "entry_price", 0.0),
            "sl_price": getattr(signal, "sl_price", 0.0),
            "tp1_price": getattr(signal, "tp1_price", 0.0),
            "tp2_price": getattr(signal, "tp2_price", 0.0),
            "preset": getattr(signal, "preset", None),
            "ema_fast": getattr(signal, "ema_fast", None),
            "ema_slow": getattr(signal, "ema_slow", None),
            "volume": getattr(signal, "volume", None),
            "volume_ma": getattr(signal, "volume_ma", None),
            "rsi": getattr(signal, "rsi", None),
            "macd": getattr(signal, "macd", None),
            "atr": getattr(signal, "atr", None),
        }
        _asyncio.create_task(reporter.report_rejected(payload, reason, mode=cfg.mode))
    except Exception:
        pass


def _simulate_exit(direction, entry, sl, tp1, klines):
    """Возвращает (exit_reason, exit_price, exit_open_time_ms) по историческим свечам."""
    direction = direction.upper()
    for k in klines:
        open_time = int(k[0])
        high = float(k[2])
        low = float(k[3])
        if direction == "LONG":
            if low <= sl:
                return "SL", sl, open_time
            if high >= tp1:
                return "TP1", tp1, open_time
        else:
            if high >= sl:
                return "SL", sl, open_time
            if low <= tp1:
                return "TP1", tp1, open_time
    return None, None, None


def _calc_simulated_pnl(direction, entry, exit_price, qty):
    """Считает реализованный PnL для симуляции (в USDT)."""
    if not entry or not exit_price or not qty:
        return 0.0
    d = direction.upper()
    if d == "LONG":
        return qty * (exit_price - entry)
    return qty * (entry - exit_price)


def _calc_simulated_qty(cfg, signal, balance):
    """Расчёт размера позиции, который был бы открыт, если бы сигнал прошёл."""
    if cfg.margin_pct > 0:
        margin = round(balance * cfg.margin_pct / 100, 1)
        raw_qty = (margin * cfg.leverage) / signal.entry_price
    elif cfg.fixed_notional_usd > 0:
        raw_qty = (cfg.fixed_notional_usd * cfg.leverage) / signal.entry_price
    elif cfg.fixed_qty > 0:
        raw_qty = cfg.fixed_qty
    elif cfg.fixed_risk_usd > 0:
        raw_qty = cfg.fixed_risk_usd / (signal.entry_price * cfg.sl_pct / 100)
    else:
        from order_manager import calc_quantity
        raw_qty = calc_quantity(
            balance=balance,
            risk_pct=cfg.risk_pct,
            sl_pct=cfg.sl_pct,
            entry_price=signal.entry_price,
            leverage=cfg.leverage,
        )
    return raw_qty


async def _load_pending_rejected(reporter, log):
    """Загружает из БД все отклонённые сделки без exit_reason в очередь симуляции."""
    if reporter is None:
        return []
    try:
        trades = await reporter.get_pending_rejected_trades()
    except Exception as e:
        log.debug(f"[REJECTED] failed to load pending trades: {e}")
        return []
    result = []
    for t in trades:
        try:
            result.append({
                "trade_id": t["id"],
                "symbol": t["symbol"],
                "direction": t.get("direction", "LONG"),
                "entry": float(t.get("entry_price", 0) or 0),
                "sl": float(t.get("sl_price", 0) or 0),
                "tp1": float(t.get("tp1_price", 0) or 0),
                "qty": float(t.get("qty", 0) or 0),
                "entry_time": t.get("entry_time"),
                "candles": 0,
                "historical_checked": False,
            })
        except Exception as e:
            log.debug(f"[REJECTED] skip pending trade {t.get('id')}: {e}")
    if result:
        log.info(f"[REJECTED] Loaded {len(result)} pending rejected trades from DB")
    return result


async def _simulate_rejected_background(client, reporter, log, shutdown_event):
    """Фоновая задача: симулирует исход отклонённых сделок по историческим свечам."""
    while not shutdown_event.is_set():
        try:
            await asyncio.sleep(60)
            if shutdown_event.is_set():
                return
            pending = [s for s in _rejected_sims if not s.get("historical_checked") and s.get("entry_time")]
            if not pending:
                continue
            by_symbol = {}
            for sim in pending:
                by_symbol.setdefault(sim["symbol"], []).append(sim)
            for symbol, sims in by_symbol.items():
                try:
                    earliest = min(s["entry_time"] for s in sims)
                    dt = datetime.fromisoformat(earliest.replace("Z", "+00:00"))
                    start_ms = int(dt.timestamp() * 1000)
                    klines = await get_recent_klines(client, symbol, "5m", start_ms, limit=500)
                    if not klines:
                        continue
                    for sim in sims:
                        try:
                            exit_reason, exit_price, exit_open_time = _simulate_exit(
                                sim["direction"], sim["entry"], sim["sl"], sim["tp1"], klines
                            )
                            if exit_reason:
                                exit_time = datetime.fromtimestamp(exit_open_time / 1000, tz=timezone.utc).isoformat()
                                qty = sim.get("qty", 0.0)
                                pnl = _calc_simulated_pnl(sim["direction"], sim["entry"], exit_price, qty)
                                commission = _sim_commission(sim["entry"], exit_price, qty)
                                net_pnl = pnl - commission
                                await reporter.patch_trade(sim["trade_id"], {
                                    "exit_price": exit_price,
                                    "exit_reason": exit_reason,
                                    "pnl": round(net_pnl, 4),
                                    "commission": round(commission, 8),
                                    "exit_time": exit_time,
                                })
                                log.info(
                                    f"[REJECTED_SIM] Trade #{sim['trade_id']} {sim['symbol']} {sim['direction']} => {exit_reason} @ {exit_price} pnl={pnl:+.4f}"
                                )
                                _rejected_sims.remove(sim)
                            else:
                                sim["historical_checked"] = True
                        except Exception as e:
                            log.debug(f"[REJECTED_SIM] error for trade {sim['trade_id']}: {e}")
                except Exception as e:
                    log.debug(f"[REJECTED_SIM] error for symbol {symbol}: {e}")
        except Exception as e:
            log.debug(f"[REJECTED_SIM] background error: {e}")


async def _simulate_rejected_outcome(current_price, reporter, log):
    """Продвигает симуляцию исходов отклонённых сигналов: если цена дошла до
    SL или TP1 фиксируем результат как exit_reason/exit_price/pnl (симулируемое,
    позиция не открывалась). Истёкшие по времени отметки убираем без результата."""
    if not _rejected_sims or reporter is None:
        return
    kept = []
    for sim in _rejected_sims:
        if sim.get("historical_checked") is False:
            # Историческая симуляция выполняется в фоновой задаче.
            kept.append(sim)
            continue
        sim["candles"] = sim.get("candles", 0) + 1
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
            import datetime as _dt
            try:
                qty = sim.get("qty", 0.0)
                pnl = _calc_simulated_pnl(direction, entry, hit_price, qty)
                commission = _sim_commission(entry, hit_price, qty)
                net_pnl = pnl - commission
                await reporter.patch_trade(tid, {
                    "exit_price": hit_price,
                    "exit_reason": hit,
                    "pnl": round(net_pnl, 4),
                    "commission": round(commission, 8),
                    "exit_time": _dt.datetime.utcnow().isoformat(),
                })
                log.info(f"[RISK_SIM] Rejected {sim.get('symbol','?')} would have HIT {hit} @ {hit_price:.4f} pnl={pnl:+.4f} (trade #{tid})")
            except Exception as e:
                log.debug(f"[RISK_SIM] finalize error: {e}")
            continue

        if sim["candles"] >= _REJECTED_SIM_MAX_CANDLES:
            log.debug(f"[RISK_SIM] Rejected trade #{tid} expired with no TP/SL")
            continue

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
        positions = await asyncio.wait_for(
            client.futures_position_information(symbol=cfg.symbol),
            timeout=30,
        )
    except asyncio.TimeoutError:
        log.error(f"[SYNC] Timeout fetching positions for {cfg.symbol}")
        return
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
                            "status": "closed",
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
                        if notifier and notifier.bot and sync_pos and ((sync_pos.mode if sync_pos else None) or cfg.mode) == "live":
                            notifier.send_message(f"🔒 CLOSED (sync) {cfg.symbol} {sync_pos.direction} | Entry={sync_pos.entry_price} PnL={pnl_val:+.4f}")
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
                            "status": "closed",
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
                                    "status": "closed",
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
    entry_timestamp_ms = None
    for p in positions:
        amt = float(p.get("positionAmt", 0))
        if abs(amt) > 0:
            direction = "LONG" if amt > 0 else "SHORT"
            entry_price = float(p.get("entryPrice", 0))
            entry_timestamp_ms = p.get("entryTime")
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

    if entry_timestamp_ms:
        entry_timestamp = pd.Timestamp(int(entry_timestamp_ms), unit="ms")
    else:
        entry_timestamp = pd.Timestamp.utcnow()

    tracker.position = Position(
        direction=direction,
        entry_price=entry_price,
        sl_price=round(sl_price, 8),
        tp1_price=round(tp1_price, 8),
        tp2_price=round(tp2_price, 8),
        total_qty=exchange_qty,
        remaining_qty=exchange_qty,
        entry_timestamp=entry_timestamp,
        mode="live",
    )

    import datetime
    mock_signal = Signal(
        direction=direction,
        entry_price=entry_price,
        sl_price=round(sl_price, 8),
        tp1_price=round(tp1_price, 8),
        tp2_price=round(tp2_price, 8),
        ema_fast=0, ema_slow=0, volume=0, volume_ma=0,
        timestamp=entry_timestamp,
        mode="live",
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
    order_mgr = OrderManager(cfg, log, client=client)
    tracker   = PositionTracker(cfg, log, reporter=reporter, order_mgr=order_mgr, notifier=notifier)
    handler   = SignalHandler(cfg, log)

    log.info("[STARTUP] Step 1: syncing position on start")
    await _sync_position_on_start(cfg, client, tracker, order_mgr, log, recovery, notifier)
    log.info("[STARTUP] Step 2: syncing done")

    log.info("[STARTUP] Step 3: reporting initial heartbeat")
    await reporter.report_heartbeat(0)
    log.info("[STARTUP] Step 4: heartbeat done")

    # Загружаем ранее отклонённые сделки из БД в очередь симуляции
    log.info("[STARTUP] Step 5: loading pending rejected trades")
    loaded = await _load_pending_rejected(reporter, log)
    _rejected_sims.extend(loaded)
    log.info(f"[STARTUP] Step 6: loaded {len(loaded)} rejected trades")

    log.info("[STARTUP] Step 7: fetching klines for warm-up")
    df_buffer: pd.DataFrame = await get_recent_klines(
        client=client, symbol=cfg.symbol, interval=cfg.timeframe,
        limit=max(cfg.ema_slow * 3, 200),
    )
    df_buffer = calculate_indicators(df_buffer, cfg)
    log.info(f"[STARTUP] Step 8: loaded {len(df_buffer)} candles for warm-up ({cfg.timeframe})")

    htf_buffer: pd.DataFrame = pd.DataFrame()
    htf_buffer_2: pd.DataFrame = pd.DataFrame()
    htf_trend_1 = None
    htf_trend_2 = None
    if cfg.htf_enabled:
        log.info("[STARTUP] Step 9: fetching HTF klines")
        htf_buffer = await get_recent_klines(
            client=client, symbol=cfg.symbol, interval=cfg.htf_timeframe,
            limit=max(cfg.htf_ema_slow * 3, 100),
        )
        htf_buffer = calculate_htf_indicators(htf_buffer, cfg)
        htf_trend_1 = get_htf_trend_latest(htf_buffer)
        log.info(f"[STARTUP] Step 10: loaded {len(htf_buffer)} HTF candles | trend={htf_trend_1}")
    else:
        log.info("[STARTUP] Step 9: HTF disabled")

    if getattr(cfg, "htf2_enabled", False):
        log.info("[STARTUP] Step 9b: fetching HTF2 klines")
        htf_buffer_2 = await get_recent_klines(
            client=client, symbol=cfg.symbol, interval=cfg.htf2_timeframe,
            limit=max(getattr(cfg, "htf2_ema_slow", 26) * 3, 100),
        )
        htf_buffer_2 = calculate_htf_indicators(
            htf_buffer_2, cfg,
            ema_fast=getattr(cfg, "htf2_ema_fast", 12),
            ema_slow=getattr(cfg, "htf2_ema_slow", 26),
        )
        htf_trend_2 = get_htf_trend_latest(htf_buffer_2)
        log.info(f"[STARTUP] Step 10b: loaded {len(htf_buffer_2)} HTF2 candles | trend={htf_trend_2}")

    log.info("[STARTUP] Step 11: starting candle polling")
    candle_count = [0]
    last_candle_time = [time.time()]
    _recent_open_times = []
    _last_signal_time = {}
    _preset_open_counts: dict[str, int] = {}
    _consecutive_losses = 0
    _last_loss_time = 0.0
    _loss_streak_reset_after = 3600  # 1 hour cooldown after streak triggers

    def _on_position_opened(preset: str):
        _preset_open_counts[preset] = _preset_open_counts.get(preset, 0) + 1

    def _on_position_closed(preset: str):
        cnt = _preset_open_counts.get(preset, 0)
        if cnt > 0:
            _preset_open_counts[preset] = cnt - 1

    async def process_hit(hit: str, current_price: float, candle_time_ms: int):
        nonlocal _consecutive_losses, _last_loss_time
        pos = tracker.position
        pos_mode = (pos.mode if pos else None) or cfg.mode
        is_live_close = (pos_mode == "live")
        is_live = (pos_mode == "live")
        if hit == "TP1" and not pos.is_recovery:
            events.info(f"TP1_HIT | price={current_price} total_qty={pos.total_qty} remaining_qty={pos.remaining_qty} old_sl={pos.sl_price}")
            preset_before = getattr(pos, 'preset', None)
            pnl = await tracker.apply_hit_async(hit, current_price, candle_time_ms)
            if preset_before and tracker.position is None:
                _on_position_closed(preset_before)
            new_sl = tracker.position.sl_price if tracker.position else 'N/A'
            events.info(f"TP1_APPLY | pnl={pnl} new_sl={new_sl} remaining_qty={tracker.position.remaining_qty if tracker.position else 0}")
            if is_live:
                notifier.send_event("tp1_hit", {
                    "symbol": cfg.symbol,
                    "direction": pos.direction,
                    "entry_price": pos.entry_price,
                    "exit_price": current_price,
                    "pnl": pnl,
                    "qty": pos.total_qty,
                })
                notifier.send_message(f"🎯 TP1 {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
            if tracker.position is not None and tracker.position.remaining_qty > 0.000001:
                await order_mgr.move_sl_to_breakeven(
                    pos.direction, pos.entry_price,
                    remaining_qty=tracker.position.remaining_qty,
                    tp2_price=pos.tp2_price,
                    mode=pos_mode,
                )
            # TP1 полностью закрыл позицию (tp1_close_pct=100) — сбрасываем глобальную
            # серию убытков, как для TP2/SL.
            if tracker.position is None or tracker.position.remaining_qty <= 0.000001:
                await recovery.report_result(pnl)
        elif hit == "TP1" and pos.is_recovery:
            events.info(f"TP1_HIT_RECOVERY | price={current_price} qty={pos.remaining_qty}")
            preset_before = getattr(pos, 'preset', None)
            pnl = await tracker.apply_hit_async(hit, current_price, candle_time_ms)
            if preset_before and tracker.position is None:
                _on_position_closed(preset_before)
            events.info(f"TP1_APPLY_RECOVERY | pnl={pnl}")
            await order_mgr.cancel_all_tp_sl(pos.direction, mode=pos_mode)
            if is_live_close:
                real_qty = await order_mgr._get_real_position_qty(pos.direction)
                if real_qty > 0 and real_qty < 0.001:
                    await order_mgr.close_dust(pos.direction, mode=pos_mode)
            if is_live:
                notifier.send_event("tp1_hit", {
                    "symbol": cfg.symbol,
                    "direction": pos.direction,
                    "entry_price": pos.entry_price,
                    "exit_price": current_price,
                    "pnl": pnl,
                    "qty": pos.total_qty,
                })
                notifier.send_message(f"🎯 TP1 [RECOVERY] {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
            await recovery.report(pnl=pnl, chain_id=pos.recovery_chain_id)
            await recovery.report_result(pnl)
        elif hit == "TP2":
            events.info(f"TP2_HIT | price={current_price} qty={pos.remaining_qty}")
            preset_before = getattr(pos, 'preset', None)
            pnl = await tracker.apply_hit_async(hit, current_price, candle_time_ms)
            if preset_before and tracker.position is None:
                _on_position_closed(preset_before)
            events.info(f"TP2_APPLY | pnl={pnl}")
            if is_live:
                notifier.send_event("tp2_hit", {
                    "symbol": cfg.symbol,
                    "direction": pos.direction,
                    "entry_price": pos.entry_price,
                    "exit_price": current_price,
                    "pnl": pnl,
                    "qty": pos.total_qty,
                })
                notifier.send_message(f"🎯 TP2 {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
            await order_mgr.cancel_all_tp_sl(pos.direction, mode=pos_mode)
            if is_live_close:
                real_qty = await order_mgr._get_real_position_qty(pos.direction)
                if real_qty > 0 and real_qty < 0.001:
                    await order_mgr.close_dust(pos.direction, mode=pos_mode)
            if pos.is_recovery:
                await recovery.report(pnl=pnl, chain_id=pos.recovery_chain_id)
            elif pnl < 0:
                await recovery.report(pnl=pnl)
            await recovery.report_result(pnl)
        else:
            events.info(f"SL_HIT | price={current_price} qty={pos.remaining_qty} tp1_hit={pos.tp1_hit}")
            preset_before = getattr(pos, 'preset', None)
            pnl = await tracker.apply_hit_async(hit, current_price, candle_time_ms)
            if preset_before and tracker.position is None:
                _on_position_closed(preset_before)
            events.info(f"SL_APPLY | pnl={pnl}")
            if is_live:
                notifier.send_event("sl_hit", {
                    "symbol": cfg.symbol,
                    "direction": pos.direction,
                    "entry_price": pos.entry_price,
                    "exit_price": current_price,
                    "pnl": pnl,
                    "qty": pos.total_qty,
                })
                notifier.send_message(f"❌ SL {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
            await order_mgr.cancel_all_tp_sl(pos.direction, mode=pos_mode)
            if is_live_close:
                real_qty = await order_mgr._get_real_position_qty(pos.direction)
                if real_qty > 0 and real_qty < 0.001:
                    await order_mgr.close_dust(pos.direction, mode=pos_mode)
            if pos.is_recovery:
                await recovery.release(chain_id=pos.recovery_chain_id)
                await recovery.report(pnl=pnl, chain_id=pos.recovery_chain_id)
                log.info(f"[RECOVERY] SL on recovery | released chain #{pos.recovery_chain_id}, new chain for loss={pnl:.4f}")
            elif pnl < 0:
                await recovery.report(pnl=pnl)
            await recovery.report_result(pnl)

        if hit == "SL":
            _consecutive_losses += 1
            _last_loss_time = time.time()
        elif hit in ("TP1", "TP2"):
            _consecutive_losses = 0
            _last_loss_time = 0.0

    async def on_candle(candle: pd.Series):
        nonlocal df_buffer
        nonlocal _consecutive_losses, _last_loss_time
        last_candle_time[0] = time.time()
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
                htf2_trend_now = get_htf_trend_latest(htf_buffer_2) if getattr(cfg, "htf2_enabled", False) else "off"
                log.info(
                    f"Heartbeat | candles={candle_count[0]} price={current_price:.2f} "
                    f"htf_trend={htf_trend_now} htf2_trend={htf2_trend_now}"
                )
                # Синхронизируем unrealized PnL с биржей для открытых позиций
                if tracker.has_open_position() and ((tracker.position.mode if tracker.position else None) or cfg.mode) == "live":
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
                if pos and candle_count[0] % 12 == 0 and ((pos.mode if pos else None) or cfg.mode) == "live":
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
                                # Определяем причину с учётом состояния TP1:
                                # если tp1_hit=True — остаток был уже в безубытке,
                                # значит это закрытие остатка по TP1, а не TP2/SL.
                                if pos.tp1_hit:
                                    hit_type = "TP1"
                                else:
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
                                preset_before = pos.preset if hasattr(pos, 'preset') else None
                                if preset_before and tracker.position is None:
                                    _on_position_closed(preset_before)
                                if hit_type == "TP2":
                                    if (pos.mode if pos else None) == "live" or cfg.mode == "live":
                                        notifier.send_event("tp2_hit", {"symbol": cfg.symbol, "direction": pos.direction, "entry_price": pos.entry_price, "exit_price": current_price, "pnl": pnl, "qty": pos.total_qty})
                                        notifier.send_message(f"🎯 TP2 {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
                                elif hit_type == "TP1":
                                    if (pos.mode if pos else None) == "live" or cfg.mode == "live":
                                        notifier.send_event("tp1_hit", {"symbol": cfg.symbol, "direction": pos.direction, "entry_price": pos.entry_price, "exit_price": current_price, "pnl": pnl, "qty": pos.total_qty})
                                        notifier.send_message(f"🎯 TP1 {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
                                elif hit_type == "SL":
                                    if (pos.mode if pos else None) == "live" or cfg.mode == "live":
                                        notifier.send_event("sl_hit", {"symbol": cfg.symbol, "direction": pos.direction, "entry_price": pos.entry_price, "exit_price": current_price, "pnl": pnl, "qty": pos.total_qty})
                                        notifier.send_message(f"❌ SL {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
                                # Отменяем оставшиеся ордера на бирже
                                await order_mgr.cancel_all_tp_sl(pos.direction, mode=(pos.mode if pos else None) or cfg.mode)
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
                    await process_hit(hit, current_price, candle_time_ms)

            # Защита от открытия второй позиции по той же монете.
            # Если после обработки сигналов TP/SL позиция всё ещё отслеживается
            # как открытая (например, после частичного TP1, когда remaining_qty > 0),
            # НЕ открываем новую сделку — иначе на бирже (one-way mode) две сделки
            # сольются в одну, а в дашборде появится дубль.
            if tracker.has_open_position():
                log.debug(f"[GUARD] Position still open for {cfg.symbol} — skip new signal")
                return

            htf_trend = None
            if cfg.htf_enabled and getattr(cfg, "htf2_enabled", False):
                t1 = get_htf_trend_latest(htf_buffer)
                t2 = get_htf_trend_latest(htf_buffer_2)
                if t1 and t2 and t1 == t2:
                    htf_trend = t1
                else:
                    log.debug(f"[HTF2] Skip signal: htf={t1} htf2={t2}")
                    return
            elif cfg.htf_enabled:
                htf_trend = get_htf_trend_latest(htf_buffer)
            elif getattr(cfg, "htf2_enabled", False):
                htf_trend = get_htf_trend_latest(htf_buffer_2)
            signals = get_all_signals(df_buffer, cfg, htf_trend, cfg.enabled_presets)
            if not signals:
                return

            # Pick best signal by volume (strongest conviction)
            signals.sort(key=lambda s: s.volume, reverse=True)
            raw_signal = signals[0]

            now = time.time()
            if cfg.signal_cooldown_min > 0:
                last_sig = _last_signal_time.get(cfg.symbol, 0)
                if now - last_sig < cfg.signal_cooldown_min * 60:
                    log.debug(f"[COOLDOWN] Skip signal for {cfg.symbol}: {now - last_sig:.0f}s < {cfg.signal_cooldown_min}m")
                    await _track_skipped_signal(reporter, raw_signal, cfg, "skip:cooldown")
                    return

            if cfg.max_open_per_cycle > 0:
                cutoff = now - 3600
                _recent_open_times[:] = [t for t in _recent_open_times if t > cutoff]
                if len(_recent_open_times) >= cfg.max_open_per_cycle:
                    log.debug(f"[CYCLE_LIMIT] Skip signal for {cfg.symbol}: {len(_recent_open_times)} opens in last 1h >= max_open_per_cycle={cfg.max_open_per_cycle}")
                    await _track_skipped_signal(reporter, raw_signal, cfg, "skip:cycle_limit")
                    return

            # Per-preset limit check
            preset_cfg = get_preset_config(raw_signal.preset)
            max_per_preset = preset_cfg.get("max_per_preset", 3)
            current_preset_count = _preset_open_counts.get(raw_signal.preset, 0)
            if max_per_preset > 0 and current_preset_count >= max_per_preset:
                log.debug(f"[PRESET_LIMIT] Skip {raw_signal.preset} for {cfg.symbol}: {current_preset_count} >= max_per_preset={max_per_preset}")
                await _track_skipped_signal(reporter, raw_signal, cfg, "skip:preset_limit")
                return

            # Loss streak protection: skip next signal(s) after consecutive losses
            if _consecutive_losses >= 3 and _last_loss_time > 0:
                if time.time() - _last_loss_time >= _loss_streak_reset_after:
                    _consecutive_losses = 0
                    _last_loss_time = 0.0
                    log.info(f"[LOSS_STREAK] Cooldown passed, resetting consecutive losses counter")
            if _consecutive_losses >= 7:
                log.debug(f"[LOSS_STREAK] Skip signal for {cfg.symbol}: {_consecutive_losses} consecutive losses >= 7")
                await _track_skipped_signal(reporter, raw_signal, cfg, "skip:loss_streak_7")
                return
            if _consecutive_losses >= 5:
                log.debug(f"[LOSS_STREAK] Skip signal for {cfg.symbol}: {_consecutive_losses} consecutive losses >= 5")
                await _track_skipped_signal(reporter, raw_signal, cfg, "skip:loss_streak_5")
                return
            if _consecutive_losses >= 3:
                log.debug(f"[LOSS_STREAK] Skip signal for {cfg.symbol}: {_consecutive_losses} consecutive losses >= 3")
                await _track_skipped_signal(reporter, raw_signal, cfg, "skip:loss_streak_3")
                return

            signal = raw_signal
            signal_data = {
                "direction": signal.direction,
                "symbol": cfg.symbol,
                "entry_price": signal.entry_price,
                "sl_price": signal.sl_price,
                "tp1_price": signal.tp1_price,
                "tp2_price": signal.tp2_price,
                "preset": signal.preset,
                "ema_fast": signal.ema_fast,
                "ema_slow": signal.ema_slow,
                "volume": signal.volume,
                "volume_ma": signal.volume_ma,
                "rsi": signal.rsi,
                "macd": signal.macd,
                "macd_signal": signal.macd_signal,
                "macd_hist": signal.macd_hist,
                "bb_upper": signal.bb_upper,
                "bb_middle": signal.bb_middle,
                "bb_lower": signal.bb_lower,
                "atr": signal.atr,
                "quote_volume": getattr(signal, "quote_volume", 0.0) or 0.0,
                "leverage": cfg.leverage,
            }

            # Optional LLM validation
            if getattr(cfg, "llm_enabled", False):
                try:
                    from llm_client import LLMClient, LLMConfig
                    llm_cfg = LLMConfig(
                        enabled=True,
                        mock=getattr(cfg, "llm_mock", False),
                        api_key=getattr(cfg, "llm_api_key", ""),
                        model=getattr(cfg, "llm_model", "llama-3.1-70b-versatile"),
                        fallback_models=getattr(cfg, "llm_fallback_models", ""),
                        gemini_api_key=getattr(cfg, "gemini_api_key", ""),
                        gemini_model=getattr(cfg, "gemini_model", "gemini-2.0-flash-exp"),
                        groq_api_key=getattr(cfg, "groq_api_key", ""),
                        groq_model=getattr(cfg, "groq_model", "groq/compound-mini"),
                        confidence_threshold=getattr(cfg, "llm_confidence_threshold", 0.7),
                        calls_per_min=getattr(cfg, "llm_calls_per_min", 20),
                        per_symbol_cooldown_min=getattr(cfg, "llm_per_symbol_cooldown_min", 5),
                        backoff_sec=getattr(cfg, "llm_backoff_sec", 60.0),
                        short_backoff_sec=getattr(cfg, "llm_short_backoff_sec", 5.0),
                        provider_retry_delay_sec=getattr(cfg, "llm_provider_retry_delay_sec", 1.0),
                    )
                    llm = LLMClient(llm_cfg)
                    indicators = {
                        "rsi": signal.rsi,
                        "macd": signal.macd,
                        "macd_hist": signal.macd_hist,
                        "atr": signal.atr,
                        "bb_lower": signal.bb_lower,
                        "bb_upper": signal.bb_upper,
                        "bb_middle": signal.bb_middle,
                        "volume": signal.volume,
                        "volume_ma": signal.volume_ma,
                        "ema_fast": signal.ema_fast,
                        "ema_slow": signal.ema_slow,
                    }
                    llm_result = await llm.validate(
                        symbol=cfg.symbol,
                        direction=signal.direction,
                        preset=signal.preset,
                        entry_price=signal.entry_price,
                        sl_price=signal.sl_price,
                        tp_price=signal.tp1_price,
                        indicators=indicators,
                    )
                    if llm_result is False:
                        log.info(f"[LLM] Signal REJECTED for {cfg.symbol} {signal.preset}")
                        signal_data["reject_reason"] = "llm_reject"
                        if reporter is not None:
                            await reporter.report_rejected(signal_data, "llm_reject", mode=cfg.mode)
                        return
                    elif llm_result is True:
                        log.info(f"[LLM] Signal APPROVED for {cfg.symbol} {signal.preset}")
                    else:
                        log.debug(f"[LLM] Signal SKIPPED (no providers) for {cfg.symbol} {signal.preset}")
                except Exception as e:
                    log.warning(f"[LLM] Validation error: {e} — proceeding without LLM")

            confirmed = await handler.confirm(signal)
            if not confirmed:
                return

            # Глобальный риск-контроль: лимит позиций + пауза после серии убытков.
            # Проверяем ДО claim, чтобы не захватывать recovery-цепочку впустую.
            if recovery:
                risk_check = await recovery.can_open()
                if not risk_check.get("allowed", True):
                    reason = risk_check.get("reason", "risk_block")
                    # Пауза из-за серии убытков = только если причина "pause" и счётчик убытков > 0.
                    # Отмечаем явно, чтобы в аналитике было видно, сколько прибыльных сигналов потеряно из-за защиты.
                    if reason == "pause" and (risk_check.get("loss_streak") or 0) > 0:
                        reject_key = "risk:loss_streak"
                    else:
                        reject_key = f"risk:{reason}"
                    log.info(
                        f"[RISK] Skip signal for {cfg.symbol}: reason={reason} ({reject_key}) "
                        f"loss_streak={risk_check.get('loss_streak')} "
                        f"(positions={risk_check.get('positions_open')})"
                    )
                    if reporter is not None:
                        balance = await order_mgr.get_balance(mode=signal.mode or cfg.mode)
                        sim_qty = _calc_simulated_qty(cfg, signal, balance)
                        signal_data["qty"] = sim_qty
                        tid = await reporter.report_rejected(signal_data, reject_key, qty=sim_qty, mode=cfg.mode)
                        if tid:
                            _rejected_sims.append({
                                "trade_id": tid,
                                "symbol": cfg.symbol,
                                "direction": signal_data.get("direction"),
                                "entry": signal_data.get("entry_price"),
                                "sl": signal_data.get("sl_price"),
                                "tp1": signal_data.get("tp1_price"),
                                "qty": sim_qty,
                                "entry_time": datetime.utcnow().isoformat(),
                                "candles": 0,
                                "historical_checked": False,
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
                    balance = await order_mgr.get_balance(mode=signal.mode or cfg.mode)
                    sim_qty = _calc_simulated_qty(cfg, signal, balance)
                    signal_data["qty"] = sim_qty
                    tid = await reporter.report_rejected(signal_data, "risk:debt_limit", qty=sim_qty, mode=cfg.mode)
                    if tid:
                        _rejected_sims.append({
                            "trade_id": tid,
                            "symbol": cfg.symbol,
                            "direction": signal_data.get("direction"),
                            "entry": signal_data.get("entry_price"),
                            "sl": signal_data.get("sl_price"),
                            "tp1": signal_data.get("tp1_price"),
                            "qty": sim_qty,
                            "entry_time": datetime.utcnow().isoformat(),
                            "candles": 0,
                            "historical_checked": False,
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
                if (signal.mode or cfg.mode) == "live" and order_mgr:
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

            result = await order_mgr.open_position(signal, recovery_target=recovery_target, mode=signal.mode or cfg.mode)
            if result is not None:
                entry_price, qty = result[0], result[1]
                signal_data["qty"] = qty
                signal.entry_price = entry_price
                is_recovery = recovery_target is not None
                if is_recovery and len(result) > 2:
                    signal.tp1_price = result[2]
                    signal.tp2_price = result[2]  # no TP2 for recovery
                # Apply per-preset TP/SL overrides (recovery keeps its own levels)
                if not is_recovery:
                    preset_cfg = get_preset_config(signal.preset)
                    if preset_cfg.get("tp"):
                        tp_pct = preset_cfg["tp"]
                        sl_pct = preset_cfg.get("sl", cfg.sl_pct)
                        atr_abs = getattr(signal, "atr", 0) or 0
                        dynamic_sl, dynamic_tp = _calc_atr_sl_tp(entry_price, atr_abs, sl_pct, tp_pct)
                        sl_dist = entry_price * dynamic_sl / 100
                        tp_dist = entry_price * dynamic_tp / 100
                        if signal.direction == "LONG":
                            signal.sl_price = round(entry_price - sl_dist, 8)
                            signal.tp1_price = round(entry_price + tp_dist, 8)
                            signal.tp2_price = round(entry_price + tp_dist, 8)
                        else:
                            signal.sl_price = round(entry_price + sl_dist, 8)
                            signal.tp1_price = round(entry_price - tp_dist, 8)
                            signal.tp2_price = round(entry_price - tp_dist, 8)
                        signal_data["sl_price"] = signal.sl_price
                        signal_data["tp1_price"] = signal.tp1_price
                        signal_data["tp2_price"] = signal.tp2_price
                await tracker.open_async(
                    signal, qty=qty,
                    is_recovery=is_recovery,
                    recovery_chain_id=chain_id,
                )
                events.info(f"POSITION_OPEN | {signal.direction} {cfg.symbol} preset={signal.preset} entry={entry_price} qty={qty} is_recovery={is_recovery} chain_id={chain_id}")
                if (getattr(signal, 'mode', None) or cfg.mode) == "live":
                    notifier.send_signal(signal_data)
                _recent_open_times.append(now)
                _last_signal_time[cfg.symbol] = now
                _on_position_opened(signal.preset)
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

    async def on_htf_candle_2(candle: pd.Series):
        nonlocal htf_buffer_2
        try:
            new_row = pd.DataFrame([candle]).set_index("open_time")
            htf_buffer_2 = pd.concat([htf_buffer_2, new_row]).tail(300)
            htf_buffer_2 = calculate_htf_indicators(
                htf_buffer_2, cfg,
                ema_fast=getattr(cfg, "htf2_ema_fast", 12),
                ema_slow=getattr(cfg, "htf2_ema_slow", 26),
            )
            trend = get_htf_trend_latest(htf_buffer_2)
            log.info(f"HTF2 candle closed | {cfg.htf2_timeframe} trend={trend}")
        except Exception as e:
            log.error(f"on_htf_candle_2 error: {e}", exc_info=True)

    async def periodic_position_check():
        """Проверка состояния позиции каждые 1 час (3600 секунд)."""
        while not shutdown_event.is_set():
            try:
                await asyncio.sleep(3600)  # 1 час
                if shutdown_event.is_set():
                    return
                    
                if tracker.has_open_position():
                    pos = tracker.position
                    if pos and ((pos.mode if pos else None) or cfg.mode) == "live":
                        exchange_pos = await order_mgr.get_position_info()
                        if exchange_pos:
                            local_qty = pos.remaining_qty
                            exchange_qty = exchange_pos.get("qty", 0)
                            
                            diff_threshold = 0.000001
                            if abs(local_qty - exchange_qty) > diff_threshold:
                                log.warning(
                                    f"[SYNC_WARNING] Position mismatch | "
                                    f"local_qty={local_qty:.6f} exchange_qty={exchange_qty:.6f} "
                                    f"direction={pos.direction}"
                                )
                                
                                if exchange_qty < diff_threshold:
                                    log.warning(
                                        f"[SYNC_WARNING] Position appears closed on exchange | "
                                        f"closing trade in DB and handling recovery"
                                    )
                                    ticker = await order_mgr.client.futures_symbol_ticker(symbol=cfg.symbol)
                                    current_price = float(ticker.get("price", 0))
                                    if current_price > 0:
                                        price_moved_favorably = (
                                            current_price > pos.entry_price if pos.direction == "LONG"
                                            else current_price < pos.entry_price
                                        )
                                        hit_type = "TP2" if price_moved_favorably else "SL"
                                    else:
                                        hit_type = "SL"
                                    candle_time_ms = int(__import__("time").time() * 1000)
                                    preset_before = getattr(pos, 'preset', None)
                                    pnl = await tracker.apply_hit_async(hit_type, current_price or pos.entry_price, candle_time_ms)
                                    if preset_before and tracker.position is None:
                                        _on_position_closed(preset_before)
                                    await order_mgr.cancel_all_tp_sl(pos.direction, mode=(pos.mode if pos else None) or cfg.mode)
                                    if pos.is_recovery:
                                        await recovery.release(chain_id=pos.recovery_chain_id)
                                        await recovery.report(pnl=pnl, chain_id=pos.recovery_chain_id)
                                        log.info(f"[RECOVERY] External close on recovery | released chain #{pos.recovery_chain_id}, new chain for loss={pnl:.4f}")
                                    elif pnl < 0:
                                        await recovery.report(pnl=pnl)
                                    await recovery.report_result(pnl)
                                else:
                                    pos.remaining_qty = exchange_qty
                                    tracker._save_state()
                        else:
                            log.warning("[SYNC_WARNING] Could not fetch position info")
            except Exception as e:
                log.warning(f"[SYNC_CHECK] Error during periodic check: {e}")

    # Запускаем периодическую проверку состояния позиции в фоне
    check_task = asyncio.create_task(periodic_position_check())
    sim_task = asyncio.create_task(_simulate_rejected_background(client, reporter, log, shutdown_event))

    async def _watchdog():
        while not shutdown_event.is_set():
            await asyncio.sleep(60)
            if shutdown_event.is_set():
                break
            if time.time() - last_candle_time[0] > 900:
                log.error("[WATCHDOG] No candles processed for 15 minutes, triggering shutdown")
                shutdown_event.set()
                break

    watchdog_task = asyncio.create_task(_watchdog())

    async def tick_sl_tp_check():
        while not shutdown_event.is_set():
            try:
                await asyncio.sleep(5)
                if shutdown_event.is_set():
                    break
                if not tracker.has_open_position():
                    continue
                try:
                    ticker = await client.futures_symbol_ticker(symbol=cfg.symbol)
                    current_price = float(ticker.get("price", 0))
                except Exception:
                    continue
                if current_price <= 0:
                    continue
                hit = tracker.check(current_price)
                if hit:
                    candle_time_ms = int(time.time() * 1000)
                    await process_hit(hit, current_price, candle_time_ms)
            except Exception as e:
                log.debug(f"[TICK_SL_TP] error: {e}")

    tick_task = asyncio.create_task(tick_sl_tp_check())

    async def _time_profit_close_check():
        while not shutdown_event.is_set():
            try:
                await asyncio.sleep(1800)
                if shutdown_event.is_set():
                    break
                if cfg.time_profit_close_hours <= 0:
                    continue
                if not tracker.has_open_position():
                    continue
                pos = tracker.position
                if not pos or pos.closed or not pos.opened_at:
                    continue
                pos_mode = (pos.mode if pos else None) or cfg.mode
                try:
                    ticker = await client.futures_symbol_ticker(symbol=cfg.symbol)
                    current_price = float(ticker.get("price", 0))
                except Exception:
                    continue
                if current_price <= 0:
                    continue
                unrealized_pnl = pos.unrealized_pnl(current_price)
                if unrealized_pnl <= 0:
                    continue
                try:
                    opened_dt = datetime.datetime.fromisoformat(pos.opened_at.replace("Z", "+00:00"))
                    age_hours = (datetime.datetime.now(datetime.timezone.utc) - opened_dt).total_seconds() / 3600
                except (ValueError, AttributeError):
                    continue
                if age_hours < cfg.time_profit_close_hours:
                    continue
                log.info(
                    f"[TIME_PROFIT] Closing profitable position | age={age_hours:.1f}h "
                    f"pnl={unrealized_pnl:.4f} entry={pos.entry_price} price={current_price}"
                )
                if order_mgr:
                    try:
                        await order_mgr.cancel_all_tp_sl(pos.direction, mode=pos_mode)
                    except Exception:
                        pass
                    if pos_mode == "live":
                        try:
                            await order_mgr.close_position_market(pos.direction, mode=pos_mode)
                        except Exception as e:
                            log.warning(f"[TIME_PROFIT] Live close failed: {e}")
                trade_id_before = tracker._trade_id
                preset_before = getattr(pos, 'preset', None)
                pnl = await tracker.apply_hit_async("TP2", current_price, int(time.time() * 1000))
                if preset_before and tracker.position is None:
                    _on_position_closed(preset_before)
                if trade_id_before and reporter:
                    try:
                        await reporter.patch_trade(trade_id_before, {"exit_reason": "TIME_PROFIT"})
                    except Exception:
                        pass
                if recovery:
                    if pos.is_recovery:
                        await recovery.release(chain_id=pos.recovery_chain_id)
                        await recovery.report(pnl=pnl, chain_id=pos.recovery_chain_id)
                    elif pnl < 0:
                        await recovery.report(pnl=pnl)
                    await recovery.report_result(pnl)
                if pos_mode == "live":
                    notifier.send_message(
                        f"⏰ TIME_PROFIT {cfg.symbol} {pos.direction} | "
                        f"Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}"
                    )
            except Exception as e:
                log.error(f"[TIME_PROFIT] Check error: {e}", exc_info=True)

    time_profit_task = asyncio.create_task(_time_profit_close_check())

    log.info(f"Listening for candles | {cfg.symbol} {cfg.timeframe} ...")

    handlers = {cfg.timeframe: on_candle}
    if cfg.htf_enabled:
        handlers[cfg.htf_timeframe] = on_htf_candle
    if getattr(cfg, "htf2_enabled", False):
        handlers[cfg.htf2_timeframe] = on_htf_candle_2

    await start_kline_polling(
        client=client, symbol=cfg.symbol, handlers=handlers,
        logger=log, poll_seconds=10, shutdown_event=shutdown_event,
    )
    
    # Останавливаем фоновые задачи
    for task in (check_task, sim_task, watchdog_task, time_profit_task, tick_task):
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())
