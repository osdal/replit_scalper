import asyncio
import logging
import os
import sys
import signal
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import pandas as pd
from binance import AsyncClient
from binance.exceptions import BinanceAPIException
from dotenv import load_dotenv


from config import load_config
from logger import get_logger, get_events_logger
from market_data import (
    PRICE_WS_MAX_AGE_SEC,
    get_current_price,
    get_price_snapshot,
    get_recent_klines,
    start_kline_polling,
    start_kline_websocket,
)
from strategy import calculate_indicators, calculate_htf_indicators, get_all_signals, get_htf_trend_latest, Signal, _calc_atr_sl_tp
from preset_config import get_preset_config
from signal_handler import SignalHandler
from order_manager import OrderManager
from position_tracker import PositionTracker, Position, _to_epoch_ms
from backtester import run_backtest
from db_reporter import DbReporter
from recovery_client import RecoveryClient
from notifier import Notifier

_this_dir = os.path.dirname(os.path.abspath(__file__))
# Окружение бота: testnet (по умолчанию) или live. Задаётся API-сервером/лаунчером
# через BOT_ENV и разделяет конфиги, стейт, логи и lock-файлы.
BOT_ENV = (os.getenv("BOT_ENV") or "testnet").strip().lower() or "testnet"
_scoped_env = os.path.join(_this_dir, "..", f".env.{BOT_ENV}")
_load = (
    load_dotenv(_scoped_env, override=True)
    or load_dotenv(os.path.join(_this_dir, "..", ".env"))
    or load_dotenv(os.path.join(_this_dir, ".env"))
)
if not _load:
    load_dotenv()

HEARTBEAT_CANDLES = 3
LOCK_FILE_TEMPLATE = "bot.lock.{env}.{symbol}"

# Порог "пыли" по объёму — совпадает с dust-логикой ниже (abs(qty) < 0.001).
DUST_QTY = 0.001

# FIX A: виртуальные TP/SL считаются по последней ТОРГОВОЙ цене (kline k.c).
# Mark-цена используется только как fallback, если торговая отсутствует/старше
# этого порога.
LAST_PRICE_MAX_AGE_SEC = 5.0

# FIX B: сколько ждать исполнения рабочего TP-лимита обратной ноги, прежде чем
# заменить его агрессивным marketable-лимитом (ожидание идёт по тикам).
REVERSE_TP_GRACE_SEC = 10.0
# FIX B: на сколько процентов marketable-лимит пересекает стакан (заполняется
# сразу как taker).
MARKETABLE_LIMIT_OFFSET_PCT = 0.05

# Дедлайны grace-ожидания TP-лимита обратной ноги (per cycle), чтобы не
# блокировать tick-цикл sleep-loop'ом: филл ждём по тикам.
_REVERSE_TP_DEADLINES: dict = {}

# Сколько раз подряд и как долго разрешено пытаться открыть reverse-ногу в одном
# цикле, прежде чем прекратить ретраи и принудительно закрыть цикл. Нужно, чтобы
# необратимые ошибки (напр. -2027 «max position at current leverage») не
# зацикливали попытку на каждом тике, но транзиентные сбои пережили окно.
REVERSE_CHAIN_MAX_ATTEMPTS = 3
REVERSE_CHAIN_FAIL_WINDOW_SEC = 60.0
_REVERSE_CHAIN_FAILS: dict = {}  # symbol -> [count, first_ts]

# FIX C: соответствие подтверждённой причины закрытия reverse-ноги метке
# trades.exit_reason (используется как fallback, если классификатор не дал метку).
_REVERSE_CLOSED_BY_REASON = {
    "tp": "REVERSE_BE",
    "backstop": "REVERSE_BACKSTOP",
    "market": "REVERSE_MARKET",
    "chain_stop": "REVERSE_CHAIN_STOP",
}

# Глобальные переменные для отслеживания recovery-состояния
_recovery_state = {}  # {symbol: {"chainId": int, "debtAmount": float, "is_recovery": bool}}

# Cumulative loss cap для reverse-цепочки: суммируем реализованный убыток закрытых
# ног + текущий unrealized. Как soon как сумма >= N% от депозита — принудительно
# закрываем весь цикл.
_reverse_cum_loss: dict[str, float] = {}
_reverse_cum_ts: dict[str, float] = {}
_REVERSE_CUM_LOSS_PCT = float(os.getenv("REVERSE_CUM_LOSS_PCT") or "0")
_REVERSE_CUM_REF_DEPOSIT_USD = float(os.getenv("REVERSE_CUM_REF_DEPOSIT_USD") or "0")
_REVERSE_CUM_LOSS_INTERVAL = 5.0  # секунд между запросами к бирже для cumulative loss

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


def _tf_to_seconds(tf: str) -> int:
    """Переводит таймфрейм Binance (1m, 5m, 1h, 1d...) в секунды."""
    tf = str(tf).strip().lower()
    unit = tf[-1]
    try:
        num = int(tf[:-1])
    except ValueError:
        return 60
    mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}.get(unit)
    if not mult:
        return 60
    return num * mult


def _is_loss_streak_reason(sim) -> bool:
    """True, если отклонённый сигнал был отклонён из-за защиты от серии убытков
    (глобальной 'risk:loss_streak' или локальной 'skip:loss_streak_*')."""
    reason = (sim.get("reject_reason") or "")
    return reason.startswith("risk:loss_streak") or reason.startswith("skip:loss_streak")


# Rate-limit записи отклонённых сделок в БД. Причины вроде max_positions срабатывают
# на КАЖДЫЙ сигнал, пока достигнут лимит позиций, — сотни записей в минуту забивают
# API-сервер и БД. Для воронки достаточно 1 записи на (символ, причину) в окно.
_REJECT_RECORD_COOLDOWN = 60.0
_last_reject_record: dict[str, float] = {}


def _reject_should_record(symbol: str, reason: str, now_ts: float) -> bool:
    """True, если пора записать отклонение в БД (прошло больше _REJECT_RECORD_COOLDOWN)."""
    key = f"{symbol}:{reason}"
    if now_ts - _last_reject_record.get(key, 0.0) >= _REJECT_RECORD_COOLDOWN:
        _last_reject_record[key] = now_ts
        return True
    return False


async def _track_skipped_signal(reporter, signal, cfg, reason):
    """Фиксирует сигнал, пропущенный лимитами/фильтрами, как rejected trade с
    reject_reason (например 'max_positions')."""
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
        # Calculate position size that would have been opened
        from order_manager import calc_quantity
        balance = 1000  # Default balance for paper trades
        qty = calc_quantity(
            balance=balance,
            risk_pct=cfg.risk_pct,
            sl_pct=cfg.sl_pct,
            entry_price=signal.entry_price,
            leverage=cfg.leverage,
        )
        _asyncio.create_task(reporter.report_rejected(payload, reason, qty, mode=cfg.mode))
    except Exception:
        pass


# Кэш armed-статуса (live arm-gate): перезапрашиваем не чаще раза в 5 секунд.
_ARM_CACHE: dict = {"ts": 0.0, "armed": False, "symbol": None}


async def _is_armed(cfg, log) -> bool:
    """Проверяет armed-флаг бота через dashboard API (кэш 5с).

    Только для live-контура (REQUIRE_ARM=true). При ошибке запроса считаем бота
    НЕ армированным (fail-closed) — безопаснее для реальных денег.
    """
    now = time.time()
    if _ARM_CACHE.get("symbol") == cfg.symbol and (now - float(_ARM_CACHE.get("ts", 0.0))) < 5.0:
        return bool(_ARM_CACHE.get("armed"))
    api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5001/api")
    armed = False
    try:
        import requests as _sync
        r = _sync.get(f"{api_url}/bots/{cfg.symbol}", timeout=5)
        data = r.json()
        armed = bool(data.get("armed", False))
    except Exception as e:
        log.warning(f"[ARM] armed check failed: {e}")
    _ARM_CACHE.update(ts=now, armed=armed, symbol=cfg.symbol)
    return armed


# Кэш relay-only статуса: флаг хранится в БД (тумблер в дашборде), опрос раз в 5с.
_RELAY_ONLY_CACHE: dict = {"ts": 0.0, "value": None, "symbol": None}


async def _is_relay_only(cfg, log) -> bool:
    """True если бот в режиме relay-only (не открывает свои входы).

    Источник правды — флаг бота в БД (можно переключать из дашборда); при
    недоступности API падаем на env SIGNAL_RELAY_ONLY.
    """
    now = time.time()
    if _RELAY_ONLY_CACHE.get("symbol") == cfg.symbol and (now - float(_RELAY_ONLY_CACHE.get("ts", 0.0))) < 5.0:
        return bool(_RELAY_ONLY_CACHE.get("value"))
    val = None
    try:
        import requests as _sync
        api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5001/api")
        r = _sync.get(f"{api_url}/bots/{cfg.symbol}", timeout=5)
        val = bool(r.json().get("relay_only", False))
    except Exception:
        val = os.getenv("SIGNAL_RELAY_ONLY", "false").lower() == "true"
    _RELAY_ONLY_CACHE.update(ts=now, value=val, symbol=cfg.symbol)
    return bool(val)


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


def _build_signal_data(signal, cfg) -> dict:
    """Строит словарь данных сигнала для API/аналитики из объекта Signal."""
    return {
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
                "reject_reason": t.get("reject_reason"),
            })
        except Exception as e:
            log.debug(f"[REJECTED] skip pending trade {t.get('id')}: {e}")
    if result:
        log.info(f"[REJECTED] Loaded {len(result)} pending rejected trades from DB")
    return result


async def _simulate_rejected_background(client, reporter, recovery, log, shutdown_event):
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
                                # Прибыльный сигнал, отклонённый защитой от серии убытков,
                                # снимает глобальный блок (симуляция — вариант А).
                                if net_pnl >= 0 and recovery and _is_loss_streak_reason(sim):
                                    await recovery.report_result(net_pnl, simulated=True)
                                _rejected_sims.remove(sim)
                            else:
                                sim["historical_checked"] = True
                        except Exception as e:
                            log.debug(f"[REJECTED_SIM] error for trade {sim['trade_id']}: {e}")
                except Exception as e:
                    log.debug(f"[REJECTED_SIM] error for symbol {symbol}: {e}")
        except Exception as e:
            log.debug(f"[REJECTED_SIM] background error: {e}")


async def _simulate_rejected_outcome(current_price, reporter, recovery, log):
    """Продвигает симуляцию исходов отклонённых сигналов: если цена дошла до
    SL или TP1 фиксируем результат как exit_reason/exit_price/pnl (симулируемое,
    позиция не открывалась). Истёкшие по времени отметки убираем без результата.
    Прибыльный сигнал, отклонённый защитой от серии убытков, снимает блок (вариант А)."""
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
                # Прибыльный отклонённый сигнал снимает глобальный блок (вариант А).
                if net_pnl >= 0 and recovery and _is_loss_streak_reason(sim):
                    await recovery.report_result(net_pnl, simulated=True)
            except Exception as e:
                log.debug(f"[RISK_SIM] finalize error: {e}")
            continue

        if sim["candles"] >= _REJECTED_SIM_MAX_CANDLES:
            log.debug(f"[RISK_SIM] Rejected trade #{tid} expired with no TP/SL")
            continue

        kept.append(sim)
    _rejected_sims[:] = kept


def _lock_file(symbol: str) -> str:
    name = LOCK_FILE_TEMPLATE.replace("{env}", BOT_ENV).replace("{symbol}", symbol.lower())
    return os.path.join(os.path.dirname(__file__) or ".", name)
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
            # Бот запущен дашбордом detached (без своей консоли), поэтому любой
            # консольный дочерний процесс (wmic/powershell/tasklist) иначе получает
            # НОВУЮ консоль и мигает окном. CREATE_NO_WINDOW + SW_HIDE это гасят.
            win_kwargs: dict = {}
            try:
                _si = subprocess.STARTUPINFO()
                _si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                _si.wShowWindow = subprocess.SW_HIDE
                win_kwargs = {
                    "creationflags": subprocess.CREATE_NO_WINDOW,
                    "startupinfo": _si,
                }
            except Exception:
                win_kwargs = {}
            # Use wmic; fall back to Get-CimInstance (Windows 10/11) for the command line
            try:
                result = subprocess.run(
                    ["wmic", "process", "where", f"ProcessId={pid}", "get", "CommandLine", "/value"],
                    capture_output=True, text=True, timeout=5, **win_kwargs,
                )
                out = result.stdout
            except Exception:
                try:
                    result = subprocess.run(
                        ["powershell", "-NoProfile", "-Command",
                         f"Get-CimInstance Win32_Process -Filter \"ProcessId={pid}\" | Select-Object -ExpandProperty CommandLine"],
                        capture_output=True, text=True, timeout=8, **win_kwargs,
                    )
                    out = result.stdout
                except Exception:
                    result = subprocess.run(
                        ["tasklist", "/V", "/FI", f"PID eq {pid}"],
                        capture_output=True, text=True, timeout=5, **win_kwargs,
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


def _price_tol(order_mgr, reference: float) -> float:
    """Ценовой допуск: один тик символа либо 0.01% относительной погрешности."""
    tick = getattr(order_mgr, "_tick_size", None) if order_mgr else None
    return max(float(tick) if tick else 0.0, abs(reference) * 1e-4, 1e-9)


def _qty_tol(order_mgr, reference: float) -> float:
    """Объёмный допуск: один шаг лота символа либо 0.01% относительной."""
    step = getattr(order_mgr, "_step_size", None) if order_mgr else None
    return max(float(step) if step else 0.0, abs(reference) * 1e-4, 1e-9)


def _state_matches_exchange(order_mgr, pos, direction, entry_price, exchange_qty):
    """Проверяет, соответствует ли сохранённая позиция биржевой.

    Совпадение: направление, цена входа и размер (remaining_qty) в пределах
    тика/шага. При tp1_hit=True remaining_qty — это живой объём, а total_qty —
    исходный (уже частично закрыт), поэтому total_qty сверяется с биржей только
    когда TP1 ещё не срабатывал. Возвращает (matched, reason).
    """
    if pos is None:
        return False, "empty state"
    if pos.direction != direction:
        return False, f"direction state={pos.direction} exchange={direction}"
    price_tol = _price_tol(order_mgr, entry_price)
    if entry_price > 0 and abs(pos.entry_price - entry_price) > price_tol:
        return False, f"entry state={pos.entry_price} exchange={entry_price} (tol={price_tol})"
    qty_tol = _qty_tol(order_mgr, exchange_qty)
    if abs(pos.remaining_qty - exchange_qty) > qty_tol:
        return False, (
            f"remaining_qty state={pos.remaining_qty} exchange={exchange_qty} "
            f"(tol={qty_tol})"
        )
    if not pos.tp1_hit and abs(pos.total_qty - exchange_qty) > qty_tol:
        return False, (
            f"total_qty state={pos.total_qty} exchange={exchange_qty} (tol={qty_tol})"
        )
    if pos.total_qty + qty_tol < pos.remaining_qty:
        return False, (
            f"inconsistent state total_qty={pos.total_qty} < "
            f"remaining_qty={pos.remaining_qty}"
        )
    return True, ""


async def _ensure_exchange_protection(order_mgr, cfg, pos, log, tracker=None) -> None:
    """Досоздаёт недостающую биржевую защиту восстановленной из state позиции.

    Не отменяет существующие ордера: читает открытые limit/algo-ордера и
    выставляет только отсутствующий TP-limit и `botsl_` backstop. Использует
    сохранённые уровни (tp1/tp2 и sl_price). Ничего не делает для пустой позиции.
    """
    if pos is None or pos.remaining_qty < 0.000001:
        return
    symbol = cfg.symbol
    try:
        await order_mgr._get_symbol_filters()
    except Exception as e:
        log.warning(f"[SYNC] Could not load symbol filters for protection check: {e}")

    # --- TP limit, соответствующий текущей стадии позиции ---
    expected_side = "SELL" if pos.direction == "LONG" else "BUY"
    target_tp = pos.tp2_price if pos.tp1_hit else pos.tp1_price
    if not target_tp or target_tp <= 0:
        target_tp = pos.tp1_price
    tp_present = False
    try:
        open_orders = await order_mgr.client.futures_get_open_orders(symbol=symbol)
    except Exception as e:
        log.warning(f"[SYNC] Could not read open orders: {e}")
        open_orders = []
    price_tol = _price_tol(order_mgr, target_tp)
    qty_tol = _qty_tol(order_mgr, pos.remaining_qty)
    # Живой TP-лимит мог быть выставлен по tick-округлённому уровню: он
    # отличается от сохранённого state tp1_price менее чем на тик (пример:
    # state 0.42549025 ↔ reduceOnly BUY 0.4256 при tickSize=0.0001). Поэтому
    # допускаем один тик биржевого округления СВЕРХ обычного допуска —
    # фактически max(один тик, _price_tol).
    tick = float(getattr(order_mgr, "_tick_size", None) or 0.0)
    tp_tol = max(price_tol, tick)
    if tick > 0 and target_tp > 0:
        tick_rounded_tp = round(round(target_tp / tick) * tick, 12)
        tp_tol = max(tp_tol, abs(target_tp - tick_rounded_tp) + tick + 1e-9)
    for o in open_orders or []:
        try:
            if (o.get("type") or "").upper() != "LIMIT":
                continue
            if (o.get("side") or "").upper() != expected_side:
                continue
            if not _is_reduce_only(o):
                continue
            o_price = float(o.get("price", 0) or 0)
            o_qty = float(o.get("origQty", o.get("quantity", 0)) or 0)
        except (TypeError, ValueError):
            continue
        # reduceOnly LIMIT на закрывающей стороне, цена в пределах допуска,
        # объём покрывает позицию (в пределах одного шага лота) → это и есть
        # живой TP, второй выставлять нельзя.
        if (o_price > 0 and target_tp > 0 and abs(o_price - target_tp) <= tp_tol
                and o_qty > 0 and o_qty + qty_tol >= pos.remaining_qty):
            tp_present = True
            log.info(
                f"[SYNC] TP limit already present | side={expected_side} "
                f"price={target_tp} qty={pos.remaining_qty} orderPrice={o_price} "
                f"orderQty={o_qty}"
            )
            break
    if not tp_present and target_tp and target_tp > 0:
        try:
            await order_mgr._place_tp_limit(pos.direction, target_tp, pos.remaining_qty)
            log.info(
                f"[SYNC] Re-placed TP limit | side={expected_side} "
                f"price={target_tp} qty={pos.remaining_qty} (was missing)"
            )
        except BinanceAPIException as e:
            if getattr(e, "code", None) == -2022:
                # -2022 = reduceOnly отклонён: позиция уже защищена живым
                # reduceOnly TP (мэтч выше не сработал из-за округления).
                # Это не ошибка — без повторной проверки не эскалируем.
                still_present = False
                try:
                    recheck = await order_mgr.client.futures_get_open_orders(symbol=symbol)
                    for o in recheck or []:
                        if ((o.get("type") or "").upper() == "LIMIT"
                                and (o.get("side") or "").upper() == expected_side
                                and bool(o.get("reduceOnly"))):
                            still_present = True
                            break
                except Exception as e2:
                    log.warning(f"[SYNC] Could not re-check orders after -2022: {e2}")
                if still_present:
                    log.info(
                        "[SYNC] TP limit already covers the position "
                        "(reduceOnly rejected) — keeping existing order"
                    )
                else:
                    log.error(
                        "[SYNC] reduceOnly TP rejected (-2022) and no live "
                        "reduceOnly TP found — protection may be missing"
                    )
            else:
                log.error(f"[SYNC] Failed to re-place TP limit: {e}", exc_info=True)
        except Exception as e:
            log.error(f"[SYNC] Failed to re-place TP limit: {e}", exc_info=True)

    # --- Биржевой backstop ---
    key = "long" if pos.direction == "LONG" else "short"
    expected_client_algo_id = f"botsl_{symbol[:10]}_{key}"
    live_algo_id = None
    try:
        algo_orders = await order_mgr.client.futures_get_open_algo_orders(symbol=symbol)
    except Exception as e:
        log.warning(f"[SYNC] Could not read open algo orders: {e}")
        algo_orders = []
    for o in algo_orders or []:
        cid = o.get("clientAlgoId") or ""
        raw_id = o.get("algoId") or o.get("orderId")
        saved_matches = (
            pos.backstop_algo_id and raw_id is not None
            and str(raw_id) == str(pos.backstop_algo_id)
        )
        if cid == expected_client_algo_id or saved_matches:
            try:
                live_algo_id = int(raw_id)
            except (TypeError, ValueError):
                live_algo_id = None
            break
    if live_algo_id is not None:
        order_mgr.backstop_algo_id = live_algo_id
        pos.backstop_algo_id = live_algo_id
        log.info(
            f"[SYNC] Backstop algo already live | algoId={live_algo_id} "
            f"clientAlgoId={expected_client_algo_id}"
        )
    else:
        if pos.backstop_algo_id:
            log.warning(
                f"[SYNC] Saved backstop algoId={pos.backstop_algo_id} is not live — "
                f"re-placing backstop"
            )
        else:
            log.warning(
                f"[SYNC] No live backstop for restored position — placing backstop"
            )
        # Не даём _place_exchange_backstop отменять чужой/устаревший algoId.
        order_mgr.backstop_algo_id = None
        new_algo_id = await order_mgr._place_exchange_backstop(
            pos.direction, pos.sl_price, qty=pos.remaining_qty
        )
        pos.backstop_algo_id = new_algo_id
        if new_algo_id:
            log.info(
                f"[SYNC] Re-placed backstop algo | algoId={new_algo_id} "
                f"sl={pos.sl_price} clientAlgoId={expected_client_algo_id}"
            )
        else:
            log.warning(f"[SYNC] Backstop algo could not be placed | sl={pos.sl_price}")
    if tracker is not None:
        tracker._save_state()


def _stale_close_entry_ms(entry_time) -> int:
    """UTC-корректное начало окна цикла для stale-close.

    Раньше здесь был naive `fromisoformat(...).timestamp()`, который читал
    время как локальное (MSK/UTC+3): окно PnL уезжало на 3 часа назад и в
    строку попадали филлы чужих циклов (row 24167: +2.3391 вместо ≈+0.0004).
    `_to_epoch_ms` трактует naive-время как UTC (и принимает trailing 'Z').
    """
    return _to_epoch_ms(entry_time)


def _is_reduce_only(o) -> bool:
    """True только если ордер явно помечен reduceOnly (Binance может не отдавать ключ)."""
    return bool(isinstance(o, dict) and o.get("reduceOnly") is True)


def _safe_reverse_tp(direction: str, entry: float, tp) -> float:
    """Не даёт виртуальному TP встать по убыточную сторону от фактического входа.

    Если рассчитанная цель T не на прибыльной стороне (проскальзывание филла),
    возвращает безопасное значение, которое не сработает мгновенно: 0.0 для SHORT
    и большое число для LONG.
    """
    try:
        e = float(entry)
        t = float(tp)
    except (TypeError, ValueError):
        return 0.0
    ok = (t > 0 and (t < e if direction == "SHORT" else t > e))
    if ok:
        return t
    return 0.0 if direction == "SHORT" else e * 1e6


STALE_CLOSE_MAX_AGE_MS = 2 * 24 * 60 * 60 * 1000


def _stale_trade_recent(entry_time) -> bool:
    """False, если вход stale-строки старше STALE_CLOSE_MAX_AGE_MS (историю не трогаем)."""
    ms = _stale_close_entry_ms(entry_time)
    if ms <= 0:
        return True
    return (int(time.time() * 1000) - ms) <= STALE_CLOSE_MAX_AGE_MS


async def _stale_backstop_executed(cfg, order_mgr, algo_id, log) -> bool:
    """True только если биржевой backstop (botsl_*) реально исполнился.

    Модульная копия _exchange_backstop_executed: та объявлена локально внутри
    _run_live_or_paper и недоступна из _sync_position_on_start.
    """
    if not algo_id or order_mgr is None or getattr(order_mgr, "client", None) is None:
        return False
    try:
        resp = await order_mgr.client.futures_get_algo_order(algoId=int(algo_id))
    except Exception as e:
        log.debug(f"[SYNC] backstop algo status read failed | algoId={algo_id}: {e}")
        return False
    if not isinstance(resp, dict):
        return False
    status = str(resp.get("algoStatus") or resp.get("status") or "").upper()
    actual = resp.get("actualOrderId") or resp.get("actual_order_id")
    return bool(actual) and status in ("TRIGGERED", "FINISHED", "FILLED")


async def _classify_stale_close_reason(
    cfg, tracker, order_mgr, log, *,
    pos=None, direction: str = "LONG", entry_ms: int = 0,
    tp1_price: float = 0.0, tp2_price: float = 0.0,
    is_reverse: bool = False, mode: Optional[str] = None,
    backstop_algo_id=None,
) -> str:
    """Причина закрытия осиротевшей строки trades — без хардкода SL.

    paper            -> paper_close (филлов нет);
    live reverse     -> REVERSE_BACKSTOP (stop реально исполнился) /
                        REVERSE_BE (выход у TP) / REVERSE_MARKET;
    live non-reverse -> TP1 / TP2 (закрывающий филл у уровня) / stale_close.
    "SL" не выдаётся никогда: он допустим только при фактическом исполнении
    stop-ордера, а здесь биржа уже во флэте и причина из ордеров неизвестна.
    """
    eff_mode = mode or getattr(pos, "mode", None) or cfg.mode or "live"
    if eff_mode == "paper":
        return "paper_close"

    exit_price = None
    if entry_ms > 0 and tracker is not None:
        try:
            summary = await tracker._exchange_cycle_summary(entry_ms, direction)
            if summary:
                exit_price = summary.get("exit_price")
        except Exception as e:
            log.debug(f"[SYNC] stale close fill lookup failed: {e}")

    def _matches(level) -> bool:
        try:
            return bool(level and exit_price and tracker._price_close(exit_price, float(level)))
        except Exception:
            return False

    if is_reverse:
        if await _stale_backstop_executed(cfg, order_mgr, backstop_algo_id, log):
            return "REVERSE_BACKSTOP"
        if _matches(tp1_price) or _matches(tp2_price):
            return "REVERSE_BE"
        return "REVERSE_MARKET"

    # TP1 проверяем ПЕРВЫМ: в конфигах tp1_pct == tp2_pct и tp1_close_pct=100,
    # т.е. позицию закрывает именно TP1, а уровни TP1/TP2 совпадают. При обратном
    # порядке любой выход по TP помечался бы как TP2.
    if _matches(tp1_price):
        return "TP1"
    if _matches(tp2_price):
        return "TP2"

    # Сверяемся с ФАКТИЧЕСКОЙ ценой биржевого TP-лимита (Position.
    # exchange_tp_price). Уровни в трекере пересчитываются от фактического входа,
    # а ордер на бирже ставится от цены сигнала, поэтому TP на бирже может быть на
    # несколько тиков ближе к входу, чем внутренний TP1. Исполнился ордер — значит
    # это TP-выход, даже если сверка по внутреннему уровню не прошла. Покрывает и
    # старые позиции, открытые до фикса.
    ex_tp1 = float(getattr(pos, "exchange_tp_price", 0.0) or 0.0) if pos is not None else 0.0
    if ex_tp1 > 0 and _matches(ex_tp1):
        return "TP1"
    return "stale_close"


async def _close_stale_db_trade(
    cfg, tracker, order_mgr, log, *,
    api_url: str, trade_id: int, entry_time,
    direction: str = "LONG", tp1_price: float = 0.0, tp2_price: float = 0.0,
    is_reverse: bool = False, mode: Optional[str] = None,
    pos=None, backstop_algo_id=None,
) -> float:
    """Закрывает stale (is_open=1) строку trades по факту флэта на бирже.

    Live: сначала tracker-путь close_open_trade_from_exchange — он заполняет
    exit_price/qty/commission/net-pnl/exit_time из userTrades (UTC-окно). Если
    он упал — inline PATCH с расчётным PnL. Paper: филлов нет, пишем нейтральную
    причину paper_close БЕЗ exit_price. Возвращает pnl.
    """
    entry_ms = _stale_close_entry_ms(entry_time)
    exit_ms = int(time.time() * 1000)
    if entry_ms > 0:
        # Не суммируем филлы за пределами разумного окна: иначе в старую
        # открытую строку попадут сделки чужих циклов.
        exit_ms = min(exit_ms, entry_ms + STALE_CLOSE_MAX_AGE_MS)
    reason = await _classify_stale_close_reason(
        cfg, tracker, order_mgr, log,
        pos=pos, direction=direction, entry_ms=entry_ms,
        tp1_price=tp1_price, tp2_price=tp2_price,
        is_reverse=is_reverse, mode=mode, backstop_algo_id=backstop_algo_id,
    )
    is_paper = reason == "paper_close"

    pnl_val = 0.0
    if not is_paper and entry_ms > 0 and order_mgr:
        try:
            real_pnl = await order_mgr.get_realized_pnl(cfg.symbol, entry_ms, exit_ms)
            if real_pnl is not None and abs(real_pnl) > 0.0001:
                pnl_val = real_pnl
        except Exception:
            pass

    if not is_paper:
        prev_trade_id = tracker._trade_id
        ok = False
        try:
            tracker._trade_id = trade_id
            ok = await tracker.close_open_trade_from_exchange(
                exit_reason=reason, direction=direction,
            )
        except Exception as e:
            log.warning(f"[SYNC] tracker close path failed for trade #{trade_id}: {e}")
            ok = False
        finally:
            tracker._trade_id = prev_trade_id
        if ok:
            return pnl_val

    # Fallback (tracker-путь упал) либо paper: inline PATCH.
    try:
        import requests as sync_requests
        patch_body = {
            "is_open": False,
            "exit_reason": reason,
            "pnl": round(pnl_val, 4),
            "exit_time": datetime.utcnow().isoformat(),
            "status": "closed",
        }
        if is_paper:
            log.info(
                f"[SYNC] paper trade #{trade_id} closed with neutral reason={reason}; "
                f"exit_price left empty (no exchange fills)"
            )
        await asyncio.to_thread(
            sync_requests.patch, f"{api_url}/trades/{trade_id}",
            json=patch_body, timeout=5,
        )
    except Exception as e:
        log.debug(f"[SYNC] inline stale PATCH failed for trade #{trade_id}: {e}")
    return pnl_val


async def _get_cumulative_loss(symbol: str, order_mgr: OrderManager, tracker: PositionTracker) -> tuple[float, float]:
    """Возвращает (cumulative_loss, unrealized_loss) для reverse-цепочки.

    cumulative_loss — суммарный убыток цикла (realized legs).
    unrealized_loss — текущий убыток открытой позиции (unrealized_pnl), если она ниже входа.
    """
    cumulative_loss = 0.0
    unrealized_loss = 0.0
    try:
        net_realized, _, _ = await tracker.cycle_realized_net()
        if net_realized is not None and net_realized < 0:
            cumulative_loss = abs(net_realized)
    except Exception:
        pass
    try:
        pos_info = await order_mgr.get_position_info()
        if pos_info:
            upnl = float(pos_info.get("unrealized_pnl") or 0.0)
            if upnl < 0:
                unrealized_loss = abs(upnl)
    except Exception:
        pass
    return cumulative_loss, unrealized_loss


async def _check_reverse_cum_loss(
    symbol: str, order_mgr: OrderManager, tracker: PositionTracker, log
) -> bool:
    """Проверяет cumulative loss cap для reverse-цепочки.

    Returns True, если нужно принудительно закрыть цикл.
    """
    if _REVERSE_CUM_LOSS_PCT <= 0:
        return False
    if not tracker.has_open_position() or not tracker.position.is_reverse:
        _reverse_cum_loss.pop(symbol, None)
        _reverse_cum_ts.pop(symbol, None)
        return False
    now = time.time()
    last_ts = _reverse_cum_ts.get(symbol, 0.0)
    if now - last_ts < _REVERSE_CUM_LOSS_INTERVAL:
        return False
    _reverse_cum_ts[symbol] = now

    ref = _REVERSE_CUM_REF_DEPOSIT_USD
    if ref <= 0:
        try:
            ref = await order_mgr.get_balance("live")
        except Exception:
            ref = 0.0
    if ref <= 0:
        return False

    cumulative_loss, unrealized_loss = await _get_cumulative_loss(symbol, order_mgr, tracker)
    cum_loss = cumulative_loss + unrealized_loss
    threshold = ref * _REVERSE_CUM_LOSS_PCT / 100.0
    if cum_loss < threshold:
        return False

    log.error(
        f"[REVERSE] cumulative loss cap | symbol={symbol} "
        f"cum={cum_loss:+.4f} threshold={threshold:.4f} ({_REVERSE_CUM_LOSS_PCT}% of {ref:.2f}) "
        f"realized={cumulative_loss:.4f} unrealized={unrealized_loss:.4f} — force-closing"
    )
    return True


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
            # Позиции нет — снимаем осиротевший биржевой backstop из состояния.
            if sync_pos is not None and getattr(sync_pos, "backstop_algo_id", None):
                await order_mgr._cancel_exchange_backstop(sync_pos.backstop_algo_id)
            # Close stale DB trades with real PnL from Binance
            try:
                import requests as sync_requests
                api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5001/api")
                trades_resp = (await asyncio.to_thread(
                    sync_requests.get, f"{api_url}/trades?symbol={cfg.symbol}&limit=10", timeout=5
                )).json()
                for trade in (trades_resp.get("trades") or []):
                    if trade.get("is_open"):
                        trade_id = trade["id"]
                        stale_entry_time = trade.get("entry_time") or (sync_pos.entry_timestamp if sync_pos else None)
                        if not _stale_trade_recent(stale_entry_time):
                            log.warning(
                                f"[SYNC] Skipping stale trade #{trade_id} for {cfg.symbol}: "
                                f"entry older than {STALE_CLOSE_MAX_AGE_MS // 3600000}h"
                            )
                            continue
                        pnl_val = await _close_stale_db_trade(
                            cfg, tracker, order_mgr, log,
                            api_url=api_url,
                            trade_id=trade_id,
                            entry_time=stale_entry_time,
                            direction=trade.get("direction") or (sync_pos.direction if sync_pos else "LONG"),
                            tp1_price=trade.get("tp1_price") or (sync_pos.tp1_price if sync_pos else 0.0),
                            tp2_price=trade.get("tp2_price") or (sync_pos.tp2_price if sync_pos else 0.0),
                            is_reverse=bool(getattr(sync_pos, "is_reverse", False)),
                            mode=getattr(sync_pos, "mode", None) or trade.get("mode"),
                            pos=sync_pos,
                            backstop_algo_id=getattr(sync_pos, "backstop_algo_id", None),
                        )
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
                            await notifier.send_message(f"🔒 CLOSED (sync) {cfg.symbol} {sync_pos.direction} | Entry={sync_pos.entry_price} PnL={pnl_val:+.4f}")
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
                api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5001/api")
                trades_resp = (await asyncio.to_thread(
                    sync_requests.get, f"{api_url}/trades?symbol={cfg.symbol}&limit=10", timeout=5
                )).json()
                for trade in (trades_resp.get("trades") or []):
                    if trade.get("is_open"):
                        trade_id = trade["id"]
                        stale_entry_time = trade.get("entry_time")
                        if not _stale_trade_recent(stale_entry_time):
                            log.warning(
                                f"[SYNC] Skipping stale trade #{trade_id} for {cfg.symbol}: "
                                f"entry older than {STALE_CLOSE_MAX_AGE_MS // 3600000}h"
                            )
                            continue
                        pnl_val = await _close_stale_db_trade(
                            cfg, tracker, order_mgr, log,
                            api_url=api_url,
                            trade_id=trade_id,
                            entry_time=stale_entry_time,
                            direction=trade.get("direction") or "LONG",
                            tp1_price=trade.get("tp1_price") or 0.0,
                            tp2_price=trade.get("tp2_price") or 0.0,
                            mode=trade.get("mode"),
                        )
                        log.info(f"[SYNC] Closed stale trade #{trade_id} for {cfg.symbol} | pnl={pnl_val:.4f}")
                        if pnl_val < 0 and recovery:
                            await recovery.report(pnl=pnl_val)
            except Exception as e:
                log.debug(f"[SYNC] Stale trade cleanup error: {e}")
        log.info(f"[SYNC] No open position found for {cfg.symbol}")
        return

    # Определяем параметры биржевой позиции один раз — нужны и для сверки со
    # сохранённым состоянием, и для fallback-пересчёта уровней из конфига.
    direction = "LONG"
    entry_price = 0.0
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

    try:
        await order_mgr._get_symbol_filters()
    except Exception as e:
        log.warning(f"[SYNC] Could not load symbol filters for state match: {e}")

    # 1) Предпочитаем СОХРАНЁННОЕ состояние, если оно соответствует биржевой
    # позиции (direction + entry_price + размер в пределах тика/шага). Тогда
    # уровни НЕ пересчитываются из конфига — берутся сохранённые load_state().
    pos = None
    if tracker.load_state():
        pos = tracker.position
        matched, mismatch_reason = _state_matches_exchange(
            order_mgr, pos, direction, entry_price, exchange_qty
        )
        if not matched:
            log.warning(
                f"[SYNC] Saved state does not match exchange position "
                f"({mismatch_reason}) — recalculating levels from config"
            )
            tracker.position = None
            tracker._trade_id = None
            tracker._entry_fill_ms = None
            pos = None
    else:
        log.info(f"[SYNC] No saved state for {cfg.symbol} — recalculating levels from config")

    if pos is not None:
        # State соответствует бирже: сохранённые уровни уже в pos
        # (sl_price/tp1_price/tp2_price/tp1_hit/remaining_qty/total_qty/
        # realized_pnl/entry_timestamp/entry_fill_ms/preset/trade_id/
        # backstop_algo_id). Восстанавливаем известный algoId backstop, чтобы
        # последующие операции не оставили orphan/дубль.
        order_mgr.backstop_algo_id = getattr(pos, "backstop_algo_id", None)
        try:
            real_qty = await order_mgr._get_real_position_qty(pos.direction)
            if real_qty < 0.000001:
                log.warning(f"[SYNC] Exchange shows no position but state has open position — position closed externally (TP/SL), clearing state")
                # Fetch real PnL and close DB trade
                try:
                    import requests as s2
                    api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5001/api")
                    trades_resp = s2.get(f"{api_url}/trades?symbol={cfg.symbol}&limit=10", timeout=5).json()
                    for trade in (trades_resp.get("trades") or []):
                        if trade.get("is_open") and pos.entry_timestamp:
                            pnl_val = await _close_stale_db_trade(
                                cfg, tracker, order_mgr, log,
                                api_url=api_url,
                                trade_id=trade["id"],
                                entry_time=trade.get("entry_time") or pos.entry_timestamp,
                                direction=pos.direction,
                                tp1_price=pos.tp1_price,
                                tp2_price=pos.tp2_price,
                                is_reverse=bool(getattr(pos, "is_reverse", False)),
                                mode=getattr(pos, "mode", None),
                                pos=pos,
                                backstop_algo_id=getattr(pos, "backstop_algo_id", None),
                            )
                            log.info(f"[SYNC] Closed stale trade #{trade['id']} after external close | pnl={pnl_val:.4f}")
                            if pnl_val < 0 and recovery:
                                await recovery.report(pnl=pnl_val)
                                # Освобождаем захваченную recovery-цепочку, если эта позиция её держала.
                                if pos and getattr(pos, "recovery_chain_id", None):
                                    await recovery.release(chain_id=pos.recovery_chain_id)
                                    log.info(f"[SYNC] Released locked recovery chain #{pos.recovery_chain_id} after external close for {cfg.symbol}")
                except Exception:
                    pass
                # Позиции на бирже нет — снимаем осиротевший backstop.
                if getattr(pos, "backstop_algo_id", None):
                    await order_mgr._cancel_exchange_backstop(pos.backstop_algo_id)
                tracker.position = None
                tracker._clear_state()
                return
            notional = real_qty * pos.entry_price
            if notional < 1.0:
                log.warning(f"[SYNC] Dust position detected (qty={real_qty}, notional=${notional:.4f}), closing")
                await order_mgr._cancel_exchange_backstop(getattr(pos, "backstop_algo_id", None))
                await order_mgr.close_dust(pos.direction)
                tracker.position = None
                tracker._clear_state()
                return
        except Exception as e:
            log.warning(f"[SYNC] Could not verify position on exchange: {e}")

        log.info(
            f"[SYNC] Restored saved levels from state | "
            f"sl={pos.sl_price} tp1={pos.tp1_price} tp2={pos.tp2_price} "
            f"qty={pos.remaining_qty} tp1_hit={pos.tp1_hit}"
        )
        # НЕ закрываем позицию рынком при уже пробитом SL: оставляем её свечному
        # циклу (его SL-путь запускает reverse). Досоздаём недостающую биржевую
        # защиту, чтобы позиция не осталась голой до следующей свечи.
        await _ensure_exchange_protection(order_mgr, cfg, pos, log, tracker=tracker)
        return

    # 2) Fallback: state отсутствует/не совпал — пересчитываем уровни из конфига.

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
            api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5001/api")
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
        api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5001/api")
        existing = requests.get(f"{api_url}/trades?symbol={cfg.symbol}&limit=500", timeout=5).json()
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

    await _replace_tp_sl(order_mgr, tracker.position, log, tracker=tracker)


async def _replace_tp_sl(order_mgr: OrderManager, pos, log, tracker=None) -> bool:
    """Replace TP/SL orders on exchange. Always returns False (kept for the
    existing call site): a breached SL no longer forces a market close.

    Также снимает старый и ставит новый биржевой backstop; при передаче tracker
    персистит его algoId в состояние позиции, чтобы рестарт не оставил дубль."""
    if not pos or pos.remaining_qty < 0.000001:
        log.warning(f"[SYNC] No position to replace TP/SL (remaining_qty={pos.remaining_qty if pos else 0})")
        return False
    try:
        # Проверяем, не пробит ли уже уровень SL. Если пробит — НЕ закрываем
        # позицию рынком (раньше это убивало позицию на рестарте): оставляем её
        # свечному циклу, который на SL-пути откроет reverse. Ордера и backstop
        # всё равно переставляются ниже, чтобы позиция не осталась голой.
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
                # НЕ закрываем позицию рынком: виртуальный SL обрабатывает
                # свечной цикл (SL-путь запускает reverse). Просто предупреждаем
                # и продолжаем — ордера/backstop будут выставлены ниже, чтобы
                # позиция не осталась голой до следующей свечи.
                log.warning(
                    f"[SYNC] SL already breached on restore | {pos.direction} "
                    f"current={current_price:.4f} sl={pos.sl_price:.4f} — "
                    f"leaving the position for the normal SL/reverse flow"
                )
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
        # Персистим новый algoId backstop (или None, если не поставился).
        if tracker is not None and tracker.position is not None:
            tracker.position.backstop_algo_id = order_mgr.backstop_algo_id
            tracker._save_state()
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
    _log_base = os.path.basename(cfg.log_file or f"{cfg.symbol.lower()}.log")
    log = get_logger(
        log_file=os.path.join("logs", BOT_ENV, _log_base), mode=cfg.mode, symbol=cfg.symbol
    )

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
        testnet=os.getenv("BINANCE_TESTNET", "false").lower() == "true",
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

    # Плечо зажимается по максимуму символа сразу на старте (и логируется там
    # же), чтобы первое же открытие позиции не упало с -4028. _set_leverage
    # никогда не бросает, поэтому startup не может сломаться из-за этого.
    if cfg.mode == "live":
        try:
            await order_mgr._set_leverage()
        except Exception as e:
            log.warning(f"[STARTUP] leverage init failed: {e}")

    # LLM-фильтр создаётся ОДИН раз на весь цикл жизни бота: circuit breaker и
    # статус провайдеров должны сохраняться между сигналами (иначе при каждом
    # сигнале лимиты/блокировки сбрасывались бы).
    llm = None
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
            log.info(f"[LLM] LLM filter enabled | groq={llm_cfg.groq_model} gemini={llm_cfg.gemini_model} openrouter={llm_cfg.model}")
        except Exception as e:
            log.warning(f"[LLM] Failed to init LLM client: {e}")
            llm = None

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
    df_buffer: pd.DataFrame = pd.DataFrame()
    _warm_attempt = 0
    while not shutdown_event.is_set():
        try:
            df_buffer = await get_recent_klines(
                client=client, symbol=cfg.symbol, interval=cfg.timeframe,
                limit=max(cfg.ema_slow * 3, 200),
            )
            break
        except Exception as e:
            _warm_attempt += 1
            _delay = min(5 * _warm_attempt, 60)
            log.warning(
                f"[STARTUP] warm-up klines failed (attempt {_warm_attempt}): {e}; "
                f"retry in {_delay}s"
            )
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=_delay)
            except asyncio.TimeoutError:
                pass
    if shutdown_event.is_set():
        log.warning("[STARTUP] shutdown during warm-up, exiting")
        return
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

        async def _force_flat_after_reverse(direction: str, closed_by: str = "market") -> None:
            """После закрытия reverse-ноги добиваем биржевой остаток выше пыли и
            сбрасываем трекер в плоское состояние (total_qty/remaining_qty не
            должны оставаться рассинхронизированными).

            closed_by — подтверждённая причина закрытия ("tp"/"backstop"/
            "market"), используется как fallback-метка, если классификатор
            ещё не выставил _last_reverse_reason. Добивание остатка рынком —
            это bot-initiated market, т.е. REVERSE_MARKET."""
            if not is_live_close:
                return
            try:
                order_mgr._invalidate_position_cache()
                real_qty = await order_mgr._get_real_position_qty(direction)
            except Exception as e:
                log.warning(f"[REVERSE] residual check failed: {e}")
                return
            if real_qty > 0:
                if real_qty > DUST_QTY:
                    log.warning(
                        f"[REVERSE] residual detected | qty={real_qty:.6f} direction={direction}"
                    )
                    closed = False
                    try:
                        closed = await order_mgr.close_position_market(direction, mode=pos_mode)
                        if not closed:
                            closed = await order_mgr.close_dust(direction, mode=pos_mode)
                    except Exception as e:
                        log.error(f"[REVERSE] residual close failed | qty={real_qty:.6f}: {e}")
                    if closed:
                        log.info(f"[REVERSE] residual closed | qty={real_qty:.6f}")
                        # Дождаться фактического флэта, чтобы добивающий филл уже
                        # был виден в userTrades до повторной финализации строки.
                        try:
                            await tracker._verify_position_closed(direction, 5)
                        except Exception as e:
                            log.debug(f"[REVERSE] post-close verify failed: {e}")
                else:
                    await order_mgr.close_dust(direction, mode=pos_mode)
            if tracker.position is not None:
                if (tracker.position.remaining_qty > 0.0
                        or tracker.position.total_qty > 0.0):
                    log.warning(
                        f"[REVERSE] tracker not flat after close | "
                        f"total_qty={tracker.position.total_qty} "
                        f"remaining_qty={tracker.position.remaining_qty} — clearing state"
                    )
                tracker.position.total_qty = 0.0
                tracker.position.remaining_qty = 0.0
                tracker.position = None
                tracker._clear_state()
            # Остаток reverse-ноги мог добраться рынком уже ПОСЛЕ снимка
            # _exchange_cycle_summary в момент детекта TP — пересчитываем строку
            # по полному окну [вход, флэт], сохраняя текущую reverse-метку.
            reason = getattr(tracker, "_last_reverse_reason", None)
            if not reason:
                reason = _REVERSE_CLOSED_BY_REASON.get(closed_by, "REVERSE_MARKET")
            await tracker.refinalize_cycle_after_flat(reason)
            if cfg.symbol in _reverse_cum_loss:
                _reverse_cum_loss.pop(cfg.symbol, None)
                _reverse_cum_ts.pop(cfg.symbol, None)

        async def _finalize_skipped_reverse(
            leg_dir: str, reason: str, preset_before, price: float,
            candle_ms: int, report: bool,
        ) -> None:
            """FIX 2: reverse не добавил ногу (nothing to hedge / книга уже в
            ноль на T). НЕ вызываем refinalize_cycle_after_flat — в paper он
            подтягивал биржевые (live) филлы и порождал фантомную строку с чужим
            exit (#24156). Если позиция реально во флэте — финализируем как
            обычно; если нет — предупреждаем и оставляем строку как есть."""
            flat = False
            try:
                if is_live_close:
                    order_mgr._invalidate_position_cache()
                    real_q = await order_mgr._get_real_position_qty(leg_dir)
                    flat = (real_q <= 0)
                else:
                    flat = (
                        tracker.position is None
                        or tracker.position.remaining_qty <= 0.000001
                    )
            except Exception as e:
                log.warning(f"[REVERSE] skipped-reverse flat check failed: {e}")
            if not flat:
                log.warning(
                    f"[REVERSE] reverse skipped ({reason}) and position not flat "
                    f"| dir={leg_dir} — leaving trade row untouched"
                )
                events.warning(
                    f"REVERSE_SKIP | {reason} | dir={leg_dir} — "
                    f"position not flat, trade row left untouched"
                )
                return
            pnl = await tracker.apply_hit_async(
                "SL", price, candle_ms, closed_by="market"
            )
            if preset_before and tracker.position is None:
                _on_position_closed(preset_before)
            events.info(f"SL_CLOSE | reverse skipped ({reason}); pnl={pnl}")
            if not tracker.has_open_position() and cfg.symbol in _reverse_cum_loss:
                _reverse_cum_loss.pop(cfg.symbol, None)
                _reverse_cum_ts.pop(cfg.symbol, None)
            if report:
                await recovery.report_result(pnl)

        # «Отклонённая» (rejected) сделка исключается из глобального счётчика серии
        # убытков и recovery — она не должна влиять на риск-контроль (как и на статистику).
        is_rejected = bool(getattr(pos, 'reject_reason', None))
        if hit == "TP1" and not pos.is_recovery:
            events.info(f"TP1_HIT | price={current_price} total_qty={pos.total_qty} remaining_qty={pos.remaining_qty} old_sl={pos.sl_price}")
            preset_before = getattr(pos, 'preset', None)
            # FIX B: reverse-нога не форсируется рынком, пока рабочий TP-лимит
            # ещё может исполниться (grace -> marketable limit -> market).
            closed_by = None
            if pos.is_reverse:
                closed_by, _tp_px = await _ensure_reverse_tp_fill(pos)
                if closed_by is None:
                    events.info(
                        f"[TP_CLOSE] reverse TP not confirmed | tp={pos.tp1_price} "
                        f"price={current_price} — position left open"
                    )
                    return
            pnl = await tracker.apply_hit_async(
                hit, current_price, candle_time_ms, closed_by=closed_by
            )
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
                await notifier.send_message(f"🎯 TP1 {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
            if tracker.position is not None and tracker.position.remaining_qty > 0.000001:
                await order_mgr.move_sl_to_breakeven(
                    pos.direction, pos.entry_price,
                    remaining_qty=tracker.position.remaining_qty,
                    tp2_price=pos.tp2_price,
                    mode=pos_mode,
                )
            # TP1 полностью закрыл позицию (tp1_close_pct=100) — сбрасываем глобальную
            # серию убытков, как для TP2/SL. Любая ПОЛОЖИТЕЛЬНАЯ сделка (в т.ч. rejected)
            # прерывает паузу; убыточная rejected не трогает счётчик (simulated=True).
            if tracker.position is None or tracker.position.remaining_qty <= 0.000001:
                # Позиция закрыта (TP1 limit исполнился) — снимаем биржевой backstop,
                # иначе останется висячий closePosition-ордер.
                await order_mgr.cancel_all_tp_sl(pos.direction, mode=pos_mode)
                if pos.is_reverse:
                    await _force_flat_after_reverse(pos.direction, closed_by or "market")
                await recovery.report_result(pnl, simulated=is_rejected)
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
                await notifier.send_message(f"🎯 TP1 [RECOVERY] {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
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
                await notifier.send_message(f"🎯 TP2 {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
            await order_mgr.cancel_all_tp_sl(pos.direction, mode=pos_mode)
            if is_live_close:
                real_qty = await order_mgr._get_real_position_qty(pos.direction)
                if real_qty > 0 and real_qty < 0.001:
                    await order_mgr.close_dust(pos.direction, mode=pos_mode)
            if pos.is_reverse:
                await _force_flat_after_reverse(pos.direction)
            if pos.is_recovery:
                await recovery.report(pnl=pnl, chain_id=pos.recovery_chain_id)
            elif pnl < 0 and not is_rejected:
                await recovery.report(pnl=pnl)
            await recovery.report_result(pnl, simulated=is_rejected)
        else:
            events.info(f"SL_HIT | price={current_price} qty={pos.remaining_qty} tp1_hit={pos.tp1_hit}")
            preset_before = getattr(pos, 'preset', None)
            orig_direction = pos.direction
            orig_entry = pos.entry_price
            orig_qty = pos.remaining_qty
            orig_sl = pos.sl_price
            orig_trade_id = getattr(tracker, "_trade_id", None)

            if pos.is_reverse and not is_rejected:
                # ================= REVERSE CHAIN =================
                # Виртуальный SL обратной ноги сработал (рынок пошёл назад):
                # пробуем ещё один разворот к исходной стороне, сайзя ВСЮ
                # накопленную книгу + уже реализованный PnL цикла в ноль.
                preset_before = getattr(pos, 'preset', None)
                chain_step = int(getattr(pos, "reverse_chain_step", 0) or 0)
                chain_max = int(getattr(cfg, "reverse_chain_max", 2) or 0)
                # Снимаем защитные ордера текущей обратной ноги.
                try:
                    await order_mgr.cancel_all_tp_sl(pos.direction, mode=pos_mode)
                except Exception as e:
                    log.debug(f"[REVERSE] cancel chain-leg orders failed: {e}")
                # Реальное знаковое нетто N (positionAmt), вход книги A и её
                # НЕреализованный PnL U (для сайзинга FIX 1).
                unrealized = None
                position_entry = None
                try:
                    order_mgr._invalidate_position_cache()
                    real_net_qty = await order_mgr._get_real_position_qty(pos.direction)
                    real_net_entry = await order_mgr._get_real_position_entry(pos.direction)
                    pos_info = await order_mgr.get_position_info()
                    if pos_info:
                        unrealized = float(pos_info.get("unrealized_pnl") or 0.0)
                        position_entry = float(pos_info.get("entry_price") or 0.0) or None
                except Exception as e:
                    log.warning(f"[REVERSE] chain real position fetch failed: {e}")
                    real_net_qty, real_net_entry = -1.0, None
                if real_net_qty > 0:
                    N = real_net_qty if pos.direction == "LONG" else -real_net_qty
                else:
                    N = pos.remaining_qty if pos.direction == "LONG" else -pos.remaining_qty
                cycle_entry = (
                    real_net_entry if (real_net_entry and real_net_entry > 0)
                    else pos.entry_price
                )
                # Уже реализованный net-PnL цикла (реализовано − комиссия).
                NET_REALIZED, _, _ = await tracker.cycle_realized_net()
                # Если биржевые филлы/PnL недоступны (rate-limit/сбой, частый случай
                # на загруженном testnet), НЕ отдаём net_realized=None: иначе
                # open_reverse_position падает в legacy-сайзинг, который игнорирует
                # накопленный убыток цикла и открывает крошечный хедж → на шаге 3
                # цикл закрывается в минус (REVERSE_BE). Берём накопленный realized
                # из состояния позиции (реализовано на прошлых разворотах).
                if NET_REALIZED is None:
                    NET_REALIZED = float(getattr(pos, "reversed_from_pnl", 0.0) or 0.0)
                    log.warning(
                        f"[REVERSE] exchange net_realized unavailable (step={chain_step}) — "
                        f"using reversed_from_pnl={NET_REALIZED:+.4f}"
                    )
                trigger_p = (
                    pos.sl_price if (pos.sl_price and pos.sl_price > 0) else current_price
                )
                cap_reached = False
                closed_by = "market"
                if _REVERSE_CUM_LOSS_PCT > 0:
                    ref = _REVERSE_CUM_REF_DEPOSIT_USD
                    if ref <= 0:
                        try:
                            ref = await order_mgr.get_balance("live")
                        except Exception:
                            ref = 0.0
                    if ref > 0:
                        cumulative_loss, unrealized_loss = await _get_cumulative_loss(cfg.symbol, order_mgr, tracker)
                        cum_loss = cumulative_loss + unrealized_loss
                        threshold = ref * _REVERSE_CUM_LOSS_PCT / 100.0
                        if cum_loss >= threshold:
                            cap_reached = True
                            closed_by = "cum_loss_cap"
                            log.error(
                                f"[REVERSE] cumulative loss cap before open | symbol={cfg.symbol} "
                                f"cum={cum_loss:+.4f} threshold={threshold:.4f} ({_REVERSE_CUM_LOSS_PCT}% of {ref:.2f}) "
                                f"step={chain_step} — force-closing"
                            )
                if cap_reached:
                    try:
                        await order_mgr.cancel_all_tp_sl(pos.direction, mode=pos_mode)
                    except Exception as e:
                        log.debug(f"[REVERSE] cancel before cum-loss close failed: {e}")
                    pnl = await tracker.apply_hit_async(
                        "SL", current_price, candle_time_ms, closed_by=closed_by
                    )
                    if preset_before and tracker.position is None:
                        _on_position_closed(preset_before)
                    events.warning(
                        f"[REVERSE] cumulative loss cap hit | symbol={cfg.symbol} "
                        f"pnl={pnl:.4f} — force-closed"
                    )
                    if cfg.symbol in _reverse_cum_loss:
                        _reverse_cum_loss.pop(cfg.symbol, None)
                        _reverse_cum_ts.pop(cfg.symbol, None)
                    return
                reverse_result = await order_mgr.open_reverse_position(
                    original_direction=pos.direction,
                    original_entry=cycle_entry,
                    original_qty=abs(N),
                    sl_price=trigger_p,
                    mode=pos_mode,
                    net_position=N,
                    net_realized=NET_REALIZED,
                    step=chain_step,
                    unrealized=unrealized,
                    # Если биржевой entry недоступен (exchange=unavailable, частый
                    # случай при сбоях сети) — берём entry из трекера, иначе A=0 и
                    # U_eff=0 → хедж сайзится крошечным и цикл не выходит в ноль.
                    position_entry=position_entry or cycle_entry,
                )
                if reverse_result:
                    rev_entry, rev_qty, rev_tp = reverse_result
                    _REVERSE_CHAIN_FAILS.pop(cfg.symbol, None)
                    if rev_qty <= 0:
                        # FIX 1/2: нога не добавлена (книга уже выходит в ноль
                        # на T либо хеджировать нечего) — не открываем
                        # фантомную reverse-ногу и не перефинализируем строку.
                        await _finalize_skipped_reverse(
                            pos.direction, "nothing to hedge", preset_before,
                            current_price, candle_time_ms, not is_rejected,
                        )
                        return
                    if N > 0:
                        new_dir = "SHORT"
                    elif N < 0:
                        new_dir = "LONG"
                    else:
                        new_dir = "SHORT" if pos.direction == "LONG" else "LONG"
                    _tp_safe = _safe_reverse_tp(new_dir, rev_entry, rev_tp)
                    if _tp_safe != float(rev_tp or 0.0):
                        log.error(
                            f"[REVERSE] invalid reverse TP | dir={new_dir} entry={rev_entry} "
                            f"tp={rev_tp} — virtual TP disabled"
                        )
                    rev_tp = _tp_safe
                    prior_realized = NET_REALIZED if NET_REALIZED is not None else (
                        getattr(pos, "reversed_from_pnl", 0.0)
                        + tracker._calc_pnl(pos.direction, cycle_entry, trigger_p, abs(N))
                    )
                    cycle_orig_dir = getattr(pos, "reversed_from_direction", "") or pos.direction
                    cycle_orig_entry = getattr(pos, "reversed_from_entry", 0.0) or cycle_entry
                    cycle_orig_qty = getattr(pos, "reversed_from_qty", 0.0) or abs(N)
                    rev_signal = Signal(
                        direction=new_dir,
                        entry_price=rev_entry,
                        sl_price=0.0,
                        tp1_price=rev_tp,
                        tp2_price=rev_tp,
                        timestamp=pd.Timestamp.now(),
                        preset=preset_before or "reverse",
                    )
                    tracker.open(
                        rev_signal, rev_qty,
                        is_reverse=True,
                        reversed_from_pnl=prior_realized,
                        reversed_from_direction=cycle_orig_dir,
                        reversed_from_qty=cycle_orig_qty,
                        reversed_from_entry=cycle_orig_entry,
                        reverse_chain_step=chain_step + 1,
                    )
                    # Та же открытая строка trades продолжает цикл.
                    tracker._trade_id = orig_trade_id
                    await tracker.update_open_trade(new_dir, rev_entry, rev_qty)
                    tracker._save_state()
                    # Биржевой safety-net новой ноги — от её входа, как сегодня.
                    if is_live:
                        # Backstop шире ВИРТУАЛЬНОГО SL ноги (а не её входа): иначе
                        # при ступенчатом SL (вариант B, 0.8→4.3%) backstop на +2%
                        # от входа сработал бы раньше виртуального SL и порвал цепочку.
                        _rev_sl = (
                            tracker.position.sl_price
                            if tracker.position is not None else 0.0
                        ) or rev_entry
                        # --- Прототип loss-cap: цена, при которой убыток ЦИКЛА =
                        # REVERSE_LOSSCAP_PCT% реф-депозита; триггер клэмпится так,
                        # чтобы не оказаться раньше виртуального SL. Логируем всегда,
                        # стоп ставим по exact_trigger только при ENABLED=true. ---
                        _lc_pct = float(os.getenv("REVERSE_LOSSCAP_PCT") or "5")
                        _lc_ref = float(os.getenv("REVERSE_LOSSCAP_REF_DEPOSIT_USD") or "0")
                        _lc_on = (
                            (os.getenv("REVERSE_LOSSCAP_ENABLED") or "false").strip().lower()
                            == "true"
                        )
                        _lc = await order_mgr.cycle_loss_cap(
                            new_dir=new_dir, net_qty=rev_qty, entry=rev_entry,
                            net_realized=prior_realized, virtual_sl=_rev_sl,
                            pct=_lc_pct,
                            ref_deposit=(_lc_ref if _lc_ref > 0 else None),
                        )
                        _lc_exact = None
                        if _lc:
                            _lc_msg = (
                                f"[LOSSCAP] step={chain_step + 1} {new_dir} "
                                f"ref={_lc['ref']:.2f} target={_lc_pct:.1f}% "
                                f"P_cap={_lc['p_cap']:.6f} vsl={_rev_sl:.6f} "
                                f"trigger={_lc['trigger']:.6f} clamped={_lc['clamped']} "
                                f"worst={_lc['worst_pct']:.1f}% "
                                f"attainable={'yes' if _lc['attainable'] else 'NO'}"
                            )
                            log.info(_lc_msg)
                            events.info(_lc_msg)
                            if _lc_on:
                                _lc_exact = _lc["trigger"]
                        order_mgr.backstop_algo_id = await order_mgr._place_exchange_backstop(
                            new_dir, _rev_sl, qty=rev_qty, exact_trigger=_lc_exact
                        )
                        if tracker.position is not None:
                            tracker.position.backstop_algo_id = order_mgr.backstop_algo_id
                            tracker._save_state()
                    events.info(
                        f"REVERSE_CHAIN_OPEN | step={chain_step + 1}/{chain_max} "
                        f"{new_dir} entry={rev_entry:.4f} qty={rev_qty:.6f} tp={rev_tp:.4f} "
                        f"N={N} net_realized={NET_REALIZED}"
                    )
                    if is_live:
                        await notifier.send_message(
                            f"🔁 REVERSE_CHAIN step={chain_step + 1} {cfg.symbol} {new_dir} "
                            f"| Entry={rev_entry:.4f} Qty={rev_qty:.6f} TP={rev_tp:.4f}"
                        )
                else:
                    # Cap исчерпан или шаг не открылся — принудительно закрываем
                    # весь нетто. Cap → отдельная метка REVERSE_CHAIN_STOP.
                    cap_reached = chain_max > 0 and chain_step >= chain_max
                    closed_by = "chain_stop" if cap_reached else "market"
                    # FIX 2: не-cap означает, что reverse реально не открылся
                    # (nothing to hedge / ошибка). Если позиция ещё не во флэте —
                    # НЕ перефинализируем строку по чужим биржевым филлам: в paper
                    # это породило фантомный дубль #24156 с live-exit.
                    if not cap_reached:
                        flat_before = False
                        try:
                            if is_live_close:
                                order_mgr._invalidate_position_cache()
                                _rq = await order_mgr._get_real_position_qty(pos.direction)
                                flat_before = (_rq <= 0)
                            else:
                                flat_before = (
                                    tracker.position is None
                                    or tracker.position.remaining_qty <= 0.000001
                                )
                        except Exception as e:
                            log.warning(f"[REVERSE] chain flat check failed: {e}")
                        if not flat_before:
                            _now = time.time()
                            _fail = _REVERSE_CHAIN_FAILS.get(cfg.symbol)
                            if _fail is None:
                                _fail = [0, _now]
                            _fail[0] += 1
                            _REVERSE_CHAIN_FAILS[cfg.symbol] = _fail
                            _fails, _first_ts = _fail
                            _persistent = (
                                _fails >= REVERSE_CHAIN_MAX_ATTEMPTS
                                and (_now - _first_ts) >= REVERSE_CHAIN_FAIL_WINDOW_SEC
                            )
                            if not _persistent:
                                log.warning(
                                    f"[REVERSE] reverse skipped and position not flat | "
                                    f"step={chain_step}/{chain_max} N={N} "
                                    f"attempt={_fails} over {int(_now - _first_ts)}s — "
                                    f"leaving trade row untouched"
                                )
                                events.warning(
                                    f"REVERSE_SKIP | step={chain_step}/{chain_max} N={N} "
                                    f"attempt={_fails} over {int(_now - _first_ts)}s "
                                    f"— reverse skipped, position not flat, row untouched"
                                )
                                # Позиция не закрыта и строка не тронута — серию
                                # убытков не увеличиваем (учтётся при реальном close).
                                # Защита снималась перед попыткой reverse — вернём
                                # биржевой backstop, чтобы нога не осталась без стопа.
                                if is_live_close:
                                    # Вернём и TP-лимит, и backstop: защита снималась
                                    # перед попыткой reverse; без TP нога закроется по рынку.
                                    _tp_px = pos.tp1_price or pos.tp2_price or 0.0
                                    if _tp_px and _tp_px > 0:
                                        try:
                                            await order_mgr._place_tp_limit(
                                                pos.direction, _tp_px, pos.remaining_qty
                                            )
                                        except Exception as e:
                                            log.warning(f"[REVERSE] re-place TP after failed reverse: {e}")
                                    try:
                                        order_mgr.backstop_algo_id = await order_mgr._place_exchange_backstop(
                                            pos.direction, pos.entry_price, qty=pos.remaining_qty
                                        )
                                        if tracker.position is not None:
                                            tracker.position.backstop_algo_id = order_mgr.backstop_algo_id
                                            tracker._save_state()
                                    except Exception as e:
                                        log.warning(f"[REVERSE] re-place backstop after failed reverse: {e}")
                                return
                            # Reverse стабильно не открывается (напр. -2027 max position
                            # at leverage) — прекращаем ретраить каждый тик и
                            # принудительно закрываем цикл.
                            log.error(
                                f"[REVERSE] chain open failed {_fails}x over "
                                f"{int(_now - _first_ts)}s (step={chain_step}/{chain_max} "
                                f"N={N}) — force-closing to stop retry loop"
                            )
                            events.warning(
                                f"REVERSE_CHAIN_RETRY_EXHAUSTED | step={chain_step}/{chain_max} "
                                f"N={N} attempts={_fails} — force-closing"
                            )
                            _REVERSE_CHAIN_FAILS.pop(cfg.symbol, None)
                            cap_reached = True
                            # Отличаем «исчерпан лимит шагов» от «следующая нога не
                            # открылась» — в БД уйдёт cycle_close_reason=chain_failed.
                            closed_by = "chain_failed"
                    # FIX 3: явная метка REVERSE_CHAIN_STOP при force-close по cap
                    # (раньше она ставилась только в DB-метку).
                    if cap_reached:
                        log.info(
                            f"REVERSE_CHAIN_STOP | step={chain_step}/{chain_max} "
                            f"N={N} net_realized={NET_REALIZED} P={trigger_p} "
                            f"— chain limit reached, force-closing"
                        )
                        events.info(
                            f"REVERSE_CHAIN_STOP | step={chain_step}/{chain_max} "
                            f"N={N} net_realized={NET_REALIZED} P={trigger_p} "
                            f"— chain limit reached, force-closing"
                        )
                    events.warning(
                        f"[REVERSE] chain {'limit reached' if cap_reached else 'open failed'} "
                        f"(step={chain_step}/{chain_max}) — force-closing {pos.direction}"
                    )
                    try:
                        await order_mgr.cancel_all_tp_sl(pos.direction, mode=pos_mode)
                    except Exception as e:
                        log.debug(f"[REVERSE] cancel before chain force-close failed: {e}")
                    if is_live_close:
                        try:
                            closed = await order_mgr.close_position_market(pos.direction, mode=pos_mode)
                            if not closed:
                                closed = await order_mgr.close_dust(pos.direction, mode=pos_mode)
                        except Exception as e:
                            log.error(f"[REVERSE] chain force-close failed: {e}")
                    pnl = await tracker.apply_hit_async(
                        "SL", current_price, candle_time_ms, closed_by=closed_by
                    )
                    if preset_before and tracker.position is None:
                        _on_position_closed(preset_before)
                    if is_live_close:
                        await _force_flat_after_reverse(pos.direction, closed_by)
                    else:
                        await tracker.refinalize_cycle_after_flat(
                            "REVERSE_CHAIN_STOP" if cap_reached else None
                        )
                    events.info(
                        f"SL_CLOSE | chain {'stop' if cap_reached else 'fallback'}; pnl={pnl}"
                    )
                    if not is_rejected:
                        await recovery.report_result(pnl)
                _consecutive_losses += 1
                _last_loss_time = time.time()
                return

            reverse_result = None
            if not is_rejected and not pos.is_recovery:
                # Снимаем TP-ордера исходной позиции (SL на бирже не выставляется),
                # чтобы они не сработали и не закрыли позицию до разворота.
                try:
                    await order_mgr.cancel_all_tp_sl(orig_direction, mode=pos_mode)
                except Exception as e:
                    log.debug(f"[REVERSE] cancel original TP failed: {e}")
                # REVERSE sizing: сайзим от РЕАЛЬНОЙ позиции на бирже, а не от
                # tracker.remaining_qty — иначе накопленный неттинг-остаток
                # раздувает следующий reverse.
                unrealized = None
                position_entry = None
                try:
                    order_mgr._invalidate_position_cache()
                    real_rev_qty = await order_mgr._get_real_position_qty(orig_direction)
                    real_rev_entry = await order_mgr._get_real_position_entry(orig_direction)
                    pos_info = await order_mgr.get_position_info()
                    if pos_info:
                        unrealized = float(pos_info.get("unrealized_pnl") or 0.0)
                        position_entry = float(pos_info.get("entry_price") or 0.0) or None
                except Exception as e:
                    log.warning(f"[REVERSE] real position fetch failed: {e}")
                    real_rev_qty, real_rev_entry = -1.0, None
                if real_rev_qty > 0:
                    if abs(real_rev_qty - orig_qty) > DUST_QTY:
                        log.info(
                            f"[REVERSE] source qty | tracker={orig_qty:.6f} exchange={real_rev_qty:.6f}"
                        )
                    orig_qty = real_rev_qty
                else:
                    log.info(
                        f"[REVERSE] source qty | tracker={orig_qty:.6f} "
                        f"exchange=unavailable — using tracker"
                    )
                if real_rev_entry and real_rev_entry > 0:
                    log.info(
                        f"[REVERSE] source entry | tracker={pos.entry_price:.6f} "
                        f"exchange={real_rev_entry:.6f} — using exchange"
                    )
                    orig_entry = real_rev_entry
                else:
                    log.debug(
                        f"[REVERSE] source entry | tracker={pos.entry_price:.6f} "
                        f"exchange=unavailable — using tracker"
                    )
                # Знаковое нетто N исходной ноги для unified fee/profit-aware
                # сайзинга 1-го reverse (net_realized=0 — ещё ничего не закрыто).
                # exchange-недоступность U/A не мешает: U_eff = N*(P-A) по A.
                net_position = (
                    orig_qty if orig_direction == "LONG" else -orig_qty
                ) if orig_qty > 0 else None
                # Первый разворот: пробуем несколько раз. При транзиентных ошибках
                # биржи (напр. -4164/-2027, лаг цены после сбоя WS/REST) НЕ закрываем
                # первую ногу по SL сразу — иначе цикл обрывается на 1-м круге.
                reverse_result = None
                for _attempt in range(1, 4):
                    try:
                        reverse_result = await order_mgr.open_reverse_position(
                            original_direction=orig_direction,
                            original_entry=orig_entry,
                            original_qty=orig_qty,
                            sl_price=orig_sl,
                            mode=pos_mode,
                    net_position=net_position,
                    net_realized=0.0 if net_position is not None else None,
                    unrealized=unrealized,
                    # См. chain-ветку: fallback на entry трекера, иначе A=0 → U_eff=0
                    # → крошечный хедж и цикл закрывается в минус.
                    position_entry=position_entry or orig_entry,
                )
                    except Exception as _e:
                        log.warning(f"[REVERSE] 1st-leg attempt {_attempt}/3 error: {_e}")
                        reverse_result = None
                    if reverse_result:
                        break
                    if _attempt < 3:
                        log.warning(
                            f"[REVERSE] 1st-leg reverse not opened — retry {_attempt}/3 in 2s"
                        )
                        await asyncio.sleep(2.0)

            if reverse_result:
                rev_entry, rev_qty, rev_tp = reverse_result
                if rev_qty <= 0:
                    # FIX 2: хеджировать нечего — нога не добавлена, не открываем
                    # фантомную reverse-ногу и не перефинализируем строку.
                    await _finalize_skipped_reverse(
                        orig_direction, "nothing to hedge", preset_before,
                        current_price, candle_time_ms, not is_rejected,
                    )
                    return
                reverse_dir = "SHORT" if orig_direction == "LONG" else "LONG"
                _tp_safe = _safe_reverse_tp(reverse_dir, rev_entry, rev_tp)
                if _tp_safe != float(rev_tp or 0.0):
                    log.error(
                        f"[REVERSE] invalid reverse TP | dir={reverse_dir} entry={rev_entry} "
                        f"tp={rev_tp} — virtual TP disabled"
                    )
                rev_tp = _tp_safe
                rev_signal = Signal(
                    direction=reverse_dir,
                    entry_price=rev_entry,
                    sl_price=0.0,
                    tp1_price=rev_tp,
                    tp2_price=rev_tp,
                    timestamp=pd.Timestamp.now(),
                    preset=preset_before or "reverse",
                )
                # Убыток исходной ноги на уровне SL (фиксируется неттингом при
                # отправке обратного ордера) — войдёт в общий результат REVERSE.
                orig_realized_pnl = tracker._calc_pnl(orig_direction, orig_entry, current_price, orig_qty)
                tracker.open(
                    rev_signal, rev_qty,
                    is_reverse=True,
                    reversed_from_pnl=orig_realized_pnl,
                    reversed_from_direction=orig_direction,
                    reversed_from_qty=orig_qty,
                    reversed_from_entry=orig_entry,
                    reverse_chain_step=1,
                )
                # Сохраняем trade_id исходной, чтобы при закрытии reverse та же
                # запись в БД была обновлена как единый результат REVERSE.
                tracker._trade_id = orig_trade_id
                # Та же открытая строка trades теперь отражает живую (обратную)
                # ногу: direction/entry_price/qty. id и is_open сохраняются —
                # цикл закроется на этой же строке как единый REVERSE.
                await tracker.update_open_trade(reverse_dir, rev_entry, rev_qty)
                tracker._save_state()
                # Биржевой safety-net для НОВОЙ (обратной) ноги. Опорный уровень —
                # цена входа: backstop встанет на exchange_sl_backstop_pct% дальше
                # от неё. Виртуальный SL реверса (reverse_sl_pct) ведёт цепочку.
                # Только для live-ноги (в paper реального ордера на бирже нет).
                if is_live:
                    # Backstop шире ВИРТУАЛЬНОГО SL ноги (см. chain-ветку выше):
                    # при ступенчатом SL backstop от входа порвал бы цепочку.
                    _rev_sl = (
                        tracker.position.sl_price
                        if tracker.position is not None else 0.0
                    ) or rev_entry
                    order_mgr.backstop_algo_id = await order_mgr._place_exchange_backstop(
                        reverse_dir, _rev_sl, qty=rev_qty
                    )
                    if tracker.position is not None:
                        tracker.position.backstop_algo_id = order_mgr.backstop_algo_id
                        tracker._save_state()
                events.info(f"REVERSE_OPEN | {reverse_dir} entry={rev_entry:.4f} qty={rev_qty:.6f} tp={rev_tp:.4f} orig={orig_direction} {orig_qty:.6f}@{orig_entry:.4f}")
                if is_live:
                    await notifier.send_message(
                        f"🔄 REVERSE {cfg.symbol} {reverse_dir} | Entry={rev_entry:.4f} Qty={rev_qty:.6f} TP={rev_tp:.4f} Orig={orig_direction} {orig_qty:.6f}@{orig_entry:.4f}"
                    )
            else:
                # Reverse не открыт (recovery/rejected/нулевой объём/ошибка) —
                # закрываем исходную позицию как обычный SL, чтобы не зависнуть.
                # Снимаем биржевой backstop/TP, чтобы не осталось висячих ордеров.
                try:
                    await order_mgr.cancel_all_tp_sl(orig_direction, mode=pos_mode)
                except Exception as e:
                    log.debug(f"[REVERSE] cancel original orders failed: {e}")
                pnl = await tracker.apply_hit_async("SL", current_price, candle_time_ms)
                if preset_before and tracker.position is None:
                    _on_position_closed(preset_before)
                events.info(f"SL_CLOSE | no reverse opened; pnl={pnl}")

        if hit == "SL" and not is_rejected:
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

            if tracker.has_open_position() and tracker.position.is_reverse:
                should_close = await _check_reverse_cum_loss(
                    cfg.symbol, order_mgr, tracker, log
                )
                if should_close:
                    try:
                        await order_mgr.cancel_all_tp_sl(tracker.position.direction, mode=cfg.mode)
                    except Exception as e:
                        log.debug(f"[REVERSE] cancel before cum-loss close failed: {e}")
                    pnl = await tracker.apply_hit_async("SL", current_price, candle_time_ms, closed_by="cum_loss_cap")
                    if hasattr(tracker, "_on_position_closed") and tracker.position is None:
                        _on_position_closed(getattr(tracker.position, "preset", None) or "")
                    events.warning(
                        f"[REVERSE] cumulative loss cap hit | symbol={cfg.symbol} "
                        f"pnl={pnl:.4f} — force-closed"
                    )
                    await reporter.report_heartbeat(current_price)
                    await _simulate_rejected_outcome(current_price, reporter, recovery, log)
                    if tracker.has_open_position():
                        pos = tracker.position
                        await reporter.report_position({
                            "direction": pos.direction,
                            "entry_price": pos.entry_price,
                            "sl_price": pos.sl_price,
                            "tp1_price": pos.tp1_price,
                            "tp2_price": pos.tp2_price,
                            "total_qty": pos.total_qty,
                            "remaining_qty": pos.remaining_qty,
                            "tp1_hit": pos.tp1_hit,
                            "realized_pnl": pos.realized_pnl,
                        })
                    else:
                        await reporter.report_position(None)
                    _reverse_cum_loss.pop(cfg.symbol, None)
                    _reverse_cum_ts.pop(cfg.symbol, None)
                    return

            await reporter.report_heartbeat(current_price)
            await _simulate_rejected_outcome(current_price, reporter, recovery, log)
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
                if cfg.symbol in _reverse_cum_loss:
                    _reverse_cum_loss.pop(cfg.symbol, None)
                    _reverse_cum_ts.pop(cfg.symbol, None)

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
                                # FIX C: внешнее закрытие reverse-ноги. Причину
                                # определяем по биржевым свидетельствам: реально
                                # исполненный backstop, исполненный TP-лимит, иначе
                                # bot-initiated/ручное/гэп = market.
                                closed_by = None
                                if pos.is_reverse:
                                    if await _exchange_backstop_executed(pos):
                                        closed_by = "backstop"
                                        events.warning(
                                            "POSITION_SYNC | reverse backstop exercised "
                                            "→ REVERSE_BACKSTOP"
                                        )
                                    elif await _reverse_tp_limit_filled(pos):
                                        closed_by = "tp"
                                        events.warning(
                                            "POSITION_SYNC | reverse TP limit filled "
                                            "→ REVERSE_BE"
                                        )
                                    else:
                                        closed_by = "market"
                                pnl = await tracker.apply_hit_async(
                                    hit_type, current_price, candle_time_ms,
                                    closed_by=closed_by,
                                )
                                closed_qty = pos.remaining_qty
                                preset_before = pos.preset if hasattr(pos, 'preset') else None
                                if preset_before and tracker.position is None:
                                    _on_position_closed(preset_before)
                                if hit_type == "TP2":
                                    if (pos.mode if pos else None) == "live" or cfg.mode == "live":
                                        notifier.send_event("tp2_hit", {"symbol": cfg.symbol, "direction": pos.direction, "entry_price": pos.entry_price, "exit_price": current_price, "pnl": pnl, "qty": pos.total_qty})
                                        await notifier.send_message(f"🎯 TP2 {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
                                elif hit_type == "TP1":
                                    if (pos.mode if pos else None) == "live" or cfg.mode == "live":
                                        notifier.send_event("tp1_hit", {"symbol": cfg.symbol, "direction": pos.direction, "entry_price": pos.entry_price, "exit_price": current_price, "pnl": pnl, "qty": pos.total_qty})
                                        await notifier.send_message(f"🎯 TP1 {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
                                elif hit_type == "SL":
                                    if (pos.mode if pos else None) == "live" or cfg.mode == "live":
                                        notifier.send_event("sl_hit", {"symbol": cfg.symbol, "direction": pos.direction, "entry_price": pos.entry_price, "exit_price": current_price, "pnl": pnl, "qty": pos.total_qty})
                                        await notifier.send_message(f"❌ SL {cfg.symbol} {pos.direction} | Entry={pos.entry_price} Exit={current_price} PnL={pnl:+.4f}")
                                # Отменяем оставшиеся ордера на бирже
                                await order_mgr.cancel_all_tp_sl(pos.direction, mode=(pos.mode if pos else None) or cfg.mode)
                                if pos.is_recovery:
                                    await recovery.release(chain_id=pos.recovery_chain_id)
                                    await recovery.report(pnl=pnl)
                                    log.info(f"[RECOVERY] External close on recovery | released chain #{pos.recovery_chain_id}, new chain for loss={pnl:.4f}")
                                elif pnl < 0:
                                        await recovery.report(pnl=pnl)
                                await recovery.report_result(pnl)
                                # Reverse-цикл, закрытый на бирже: строка trades
                                # могла быть зафинализирована частично. Пересчёт
                                # по полному окну [вход, флэт] до fallback-закрытия.
                                if pos.is_reverse:
                                    await tracker.refinalize_cycle_after_flat(
                                        getattr(tracker, "_last_reverse_reason", None)
                                    )
                                # Fallback: биржа флэт, но строка trades могла
                                # остаться открытой (apply_hit_async не закрыл её).
                                # Метод идемпотентен: уже закрытую строку не трогает.
                                await tracker.close_open_trade_from_exchange(
                                    exit_reason="exchange_closed"
                                )
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

            # Сначала считаем сигналы БЕЗ фильтра по старшему таймфрейму (htf_trend=None —
            # HTF-фильтр выключается) и помечаем отклонённые HTF-варианты, чтобы видеть
            # причину «высший таймфрейм» в воронке сигналов.
            base_signals = get_all_signals(df_buffer, cfg, None, cfg.enabled_presets)

            # Режимно-адаптивный consensus: зависит от зоны ADX последней свечи
            try:
                adx_now = float(df_buffer.iloc[-1].get("adx", 0) or 0) if "adx" in df_buffer.columns else 0.0
            except Exception:
                adx_now = 0.0
            consensus = getattr(cfg, "min_consensus", 1)
            if adx_now < 15:
                consensus = getattr(cfg, "consensus_flat", None) or consensus
            elif adx_now < 25:
                consensus = getattr(cfg, "consensus_weak", None) or consensus
            else:
                consensus = getattr(cfg, "consensus_trend", None) or consensus

            signals = get_all_signals(df_buffer, cfg, htf_trend, cfg.enabled_presets,
                                      min_consensus=consensus)
            if not signals:
                return

            # Pick best signal by volume (strongest conviction)
            signals.sort(key=lambda s: s.volume, reverse=True)
            raw_signal = signals[0]

            # В тренде (ADX>=25) блокируем SHORT, если включено
            if adx_now >= 25 and getattr(cfg, "trend_block_short", False) and raw_signal.direction == "SHORT":
                log.debug(f"[TREND_SHORT_BLOCK] Skip SHORT {cfg.symbol} at ADX={adx_now:.1f}")
                return

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
            if getattr(cfg, "loss_streak_skip_enabled", True):
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
            signal_data = _build_signal_data(signal, cfg)

            # Optional LLM validation
            if llm is not None:
                try:
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
                    if reporter is not None:
                        await reporter.report_llm_status(llm.status.to_dict())
                    if llm_result is False:
                        log.info(f"[LLM] Signal REJECTED for {cfg.symbol} {signal.preset}")
                        signal_data["reject_reason"] = "llm_reject"
                        if reporter is not None:
                            trade_id = await reporter.report_rejected(signal_data, "llm_reject", mode=cfg.mode)
                            # Отклонённый ИИ сигнал НЕ открывает позицию в трекере (иначе
                            # rejected-позиция занимает слот и блокирует настоящие сделки
                            # через guard в on_candle). Записываем в БД для наблюдения и
                            # добавляем в очередь симуляции: фоновая задача найдёт исход
                            # (SL/TP1) по историческим свечам и допишет exit_price/pnl.
                            if trade_id:
                                qty = _calc_simulated_qty(cfg, signal, cfg.paper_balance)
                                _rejected_sims.append({
                                    "trade_id": trade_id,
                                    "symbol": cfg.symbol,
                                    "direction": signal.direction,
                                    "entry": float(signal.entry_price or 0),
                                    "sl": float(signal.sl_price or 0),
                                    "tp1": float(signal.tp1_price or 0),
                                    "qty": qty,
                                    "entry_time": datetime.now(timezone.utc).isoformat(),
                                    "candles": 0,
                                    "historical_checked": False,
                                    "reject_reason": "llm_reject",
                                })
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

            # Глобальный риск-контроль: лимит позиций (max_positions) + пауза после серии убытков.
            # Сервер считает ТОЛЬКО ПРИНЯТЫЕ открытые позиции (status != 'rejected').
            # Договорённость: ВСЕ сделки обрабатываются одинаково — если сигнал «отклонён»
            # (лимит/пауза и пр.), он ВСЁ РАВНО открывается как настоящая позиция
            # (трейкер, SL/TP, видна в боте), НО помечается rejected и не учитывается
            # в статистике/винрейте и счётчике лимита.
            open_reject_reason = None
            if recovery:
                risk_check = await recovery.can_open()
                if not risk_check.get("allowed", True):
                    reason = risk_check.get("reason", "risk_block")
                    if (reason == "loss_streak") or (
                        reason == "pause" and (risk_check.get("loss_streak") or 0) > 0
                    ):
                        reject_key = "risk:loss_streak"
                    else:
                        reject_key = f"risk:{reason}"
                    open_reject_reason = reject_key.split(":", 1)[-1]  # "max_positions"|"loss_streak"|...
                    log.info(
                        f"[RISK] {reject_key} for {cfg.symbol} — opening as REJECTED "
                        f"(excluded from winrate/PnL; positions={risk_check.get('positions_open')})"
                    )

            # Для «отклонённых» сделок recovery не задействуем (без захвата цепочки).
            if open_reject_reason:
                claim = {"chainId": None, "debtAmount": 0.0, "bonusPct": 0.0, "enabled": False}
            else:
                # Пробуем захватить свободный долг для recovery-режима
                claim = await recovery.claim()
            recovery_target = None
            chain_id = None

            # Логируем полный ответ от сервера
            log.info(f"[RECOVERY] claim response: {claim}")

            # Потолок долга: сервер отказывает в выдаче recovery-долга, когда
            # суммарный free+locked долг >= max_free_debt_usd. В этом случае
            # НЕ открываем даже обычную позицию — пропускаем сигнал, чтобы
            # не наращивать риск дальше. Rejected сделки пропускают этот лимит.
            if claim.get("reason") == "debt_limit" and not open_reject_reason:
                log.warning(
                    f"[RISK] Debt limit reached (freeDebt={claim.get('freeDebt')})"
                    f" — skipping signal for {cfg.symbol}"
                )
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

            # [GUARD] Pre-entry flat guard (только live): перед открытием новой
            # signal-позиции убеждаемся, что на бирже нет "зависшего" остатка от
            # предыдущего цикла. В one-way режиме остаток сложился бы с новым
            # ордером, и следующий reverse сайзился бы от раздутого нетто.
            entry_mode = signal.mode or cfg.mode
            if entry_mode == "live" and order_mgr:
                try:
                    order_mgr._invalidate_position_cache()
                    stale = await order_mgr.get_position_info()
                except Exception as e:
                    log.warning(f"[GUARD] stale position check failed: {e}")
                    stale = None
                if stale and abs(stale.get("qty", 0.0)) > DUST_QTY:
                    stale_dir = stale.get("direction")
                    stale_qty = abs(stale.get("qty", 0.0))
                    flattened = False
                    try:
                        flattened = await order_mgr.close_position_market(stale_dir, mode=entry_mode)
                        if not flattened:
                            flattened = await order_mgr.close_dust(stale_dir, mode=entry_mode)
                    except Exception as e:
                        log.warning(f"[GUARD] stale flatten failed: {e}")
                        flattened = False
                    if not flattened:
                        log.warning(
                            f"[GUARD] entry skipped: could not flatten stale position | "
                            f"qty={stale_qty:.6f} direction={stale_dir}"
                        )
                        return
                    log.info(
                        f"[GUARD] stale position flattened | qty={stale_qty:.6f} direction={stale_dir}"
                    )
                    # Биржа уже сфлэттенила остаток помимо логики бота. Если по
                    # этому остатку осталась открытая строка trades (is_open=1) —
                    # закрываем её по реальным филлам, иначе в дашборде будет
                    # висеть пустая открытая запись. Если соответствующей строки
                    # нет — метод ничего не пишет, логируем и продолжаем.
                    if not await tracker.close_open_trade_from_exchange(
                        exit_reason="stale_flatten",
                        direction=stale_dir,
                    ):
                        log.info(
                            f"[GUARD] stale position flattened but no matching open "
                            f"trade row to close | direction={stale_dir} "
                            f"qty={stale_qty:.6f}"
                        )

            # relay-only: свои сигналы не торгуем (вход только из testnet-релея).
            if await _is_relay_only(cfg, log):
                log.debug("[RELAY] relay-only mode — own entry skipped")
                return
            # Режим auto пока не реализован (заглушка) — новые позиции не открываем.
            if getattr(cfg, "trade_mode", "manual") == "auto":
                log.info(f"[TRADE_MODE] auto not implemented yet — entry skipped ({cfg.symbol})")
                return
            # ARM-гейт (live): торговля только после явного arm в UI.
            if os.getenv("REQUIRE_ARM", "false").lower() == "true" and not await _is_armed(cfg, log):
                log.info(f"[ARM] {cfg.symbol} not armed — entry skipped")
                await _track_skipped_signal(reporter, signal, cfg, "skip:not_armed")
                return
            # Мягкая остановка: новые входы запрещены, текущую позицию доводим по логике.
            if _stop_requested:
                log.info("[STOP] graceful stop requested — new entries disabled")
                return

            # Per-preset TP/SL overrides считаются ДО open_position.
            # Раньше они применялись после входа, от цены фактического филла, а
            # биржевые ордера к этому моменту уже стояли на уровнях, посчитанных от
            # цены сигнала. Из-за этого TP на бирже и TP1 в трекере расходились
            # (на NEARUSDT 4.868 против 4.8753): биржа закрывала позицию раньше,
            # внутренний TP1 бота не срабатывал, и выход попадал в БД как
            # stale_close вместо TP1. Теперь уровни считаются один раз и
            # используются и для биржевых ордеров, и для трекера.
            is_recovery = recovery_target is not None
            if not is_recovery:
                preset_cfg = get_preset_config(signal.preset)
                if preset_cfg.get("tp"):
                    tp_pct = preset_cfg["tp"]
                    sl_pct = preset_cfg.get("sl", cfg.sl_pct)
                    atr_abs = getattr(signal, "atr", 0) or 0
                    if signal.direction == "LONG":
                        mult = getattr(cfg, "atr_tp_multiplier_long", None) or getattr(cfg, "atr_tp_multiplier", 2.0)
                    else:
                        mult = getattr(cfg, "atr_tp_multiplier_short", None) or getattr(cfg, "atr_tp_multiplier", 2.0)
                    dynamic_sl, dynamic_tp = _calc_atr_sl_tp(signal.entry_price, atr_abs, sl_pct, tp_pct,
                                                             tp_multiplier=mult)
                    # TP2 (раннер) дальше TP1, если задан atr_tp2_multiplier
                    tp2_mult = getattr(cfg, "atr_tp2_multiplier", 0.0) or 0.0
                    dynamic_tp2 = tp2_mult * dynamic_sl if tp2_mult > 0 and dynamic_sl > 0 else dynamic_tp
                    sl_dist = signal.entry_price * dynamic_sl / 100
                    tp_dist = signal.entry_price * dynamic_tp / 100
                    tp2_dist = signal.entry_price * dynamic_tp2 / 100
                    if signal.direction == "LONG":
                        signal.sl_price = round(signal.entry_price - sl_dist, 8)
                        signal.tp1_price = round(signal.entry_price + tp_dist, 8)
                        signal.tp2_price = round(signal.entry_price + tp2_dist, 8)
                    else:
                        signal.sl_price = round(signal.entry_price + sl_dist, 8)
                        signal.tp1_price = round(signal.entry_price - tp_dist, 8)
                        signal.tp2_price = round(signal.entry_price - tp2_dist, 8)
                    signal_data["sl_price"] = signal.sl_price
                    signal_data["tp1_price"] = signal.tp1_price
                    signal_data["tp2_price"] = signal.tp2_price

            result = await order_mgr.open_position(signal, recovery_target=recovery_target, mode=signal.mode or cfg.mode)
            if result is not None:
                entry_price, qty = result[0], result[1]
                signal_data["qty"] = qty
                signal.entry_price = entry_price
                if is_recovery and len(result) > 2:
                    signal.tp1_price = result[2]
                    signal.tp2_price = result[2]  # no TP2 for recovery
                await tracker.open_async(
                    signal, qty=qty,
                    is_recovery=is_recovery,
                    recovery_chain_id=chain_id,
                    reject_reason=open_reject_reason,
                )
                # Персистим algoId биржевого backstop, выставленного order_mgr в
                # entry flow (open_position -> _place_all_orders / recovery branch),
                # чтобы рестарт не оставил orphan/дубль.
                if tracker.position is not None:
                    tracker.position.backstop_algo_id = order_mgr.backstop_algo_id
                    # Фактическая цена биржевого TP-лимита: нужна реконсилятору,
                    # чтобы выход по исполненному ордеру не помечался как stale_close.
                    tracker.position.exchange_tp_price = order_mgr.exchange_tp_price
                    tracker._save_state()
                # Релей testnet→live: публикуем вход + уровни стопа/тейков
                # (только если задан SIGNAL_RELAY_URL; live не публикует).
                if os.getenv("SIGNAL_RELAY_URL"):
                    try:
                        await reporter.publish_relay_signal({
                            "symbol": cfg.symbol,
                            "kind": "entry",
                            "direction": signal.direction,
                            "entry_price": entry_price,
                            "sl_price": signal.sl_price,
                            "tp1_price": signal.tp1_price,
                            "tp2_price": signal.tp2_price,
                            "preset": signal.preset,
                            "source": "testnet",
                        })
                    except Exception as e:
                        log.debug(f"[RELAY] publish failed: {e}")
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
                                    current_price = await _latest_price()
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
                                    # FIX C: причина внешнего закрытия reverse-ноги.
                                    closed_by = None
                                    if pos.is_reverse:
                                        if await _exchange_backstop_executed(pos):
                                            closed_by = "backstop"
                                        elif await _reverse_tp_limit_filled(pos):
                                            closed_by = "tp"
                                        else:
                                            closed_by = "market"
                                    pnl = await tracker.apply_hit_async(
                                        hit_type, current_price or pos.entry_price,
                                        candle_time_ms, closed_by=closed_by,
                                    )
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

    async def _flat_position_reconciler():
        """Рантайм-сверка (раз в 60с): если трекер держит позицию, а биржа ФЛЭТ —
        финализируем запись сделки и чистим стейт, чтобы дашборд не показывал
        фантомные открытые позиции (закрытие вне бота: backstop/флэт/ручное,
        пока процесс был перезапущен, или внешнее закрытие в рантайме).

        Безопасность: `_get_real_position_qty` → -1 при сбое опроса (НЕ трогаем),
        0 — точно нет позиции, >0 — позиция есть. Сверяем обе стороны и не трогаем
        позицию, открытую менее 120с назад (чтобы не гоняться с филом).
        """
        while not shutdown_event.is_set():
            await asyncio.sleep(60)
            if shutdown_event.is_set():
                break
            pos = tracker.position
            if pos is None or getattr(pos, "remaining_qty", 0) <= 0:
                continue
            entry_ms = getattr(pos, "entry_fill_ms", None)
            if entry_ms and (time.time() * 1000 - float(entry_ms)) < 120_000:
                continue
            try:
                order_mgr._invalidate_position_cache()
                q_dir = await order_mgr._get_real_position_qty(pos.direction)
                q_opp = await order_mgr._get_real_position_qty(
                    "SHORT" if pos.direction == "LONG" else "LONG"
                )
            except Exception as e:
                log.debug(f"[SYNC] reconciler fetch failed: {e}")
                continue
            if q_dir < 0 or q_opp < 0:
                continue  # опрос не удался — состояние неизвестно, не трогаем
            if q_dir > 1e-9 or q_opp > 1e-9:
                continue  # позиция реально есть
            log.warning(
                f"[SYNC] reconciler: exchange flat but tracker holds {pos.direction} "
                f"qty={pos.remaining_qty} — finalizing stale trade"
            )
            try:
                import requests as _rq
                api_url = os.getenv("DASHBOARD_API_URL", "http://localhost:5001/api")
                trades_resp = _rq.get(
                    f"{api_url}/trades?symbol={cfg.symbol}&limit=10", timeout=5
                ).json()
                for trade in (trades_resp.get("trades") or []):
                    if trade.get("is_open"):
                        try:
                            pnl_val = await _close_stale_db_trade(
                                cfg, tracker, order_mgr, log,
                                api_url=api_url,
                                trade_id=trade["id"],
                                entry_time=trade.get("entry_time") or pos.entry_timestamp,
                                direction=pos.direction,
                                tp1_price=pos.tp1_price,
                                tp2_price=pos.tp2_price,
                                is_reverse=bool(getattr(pos, "is_reverse", False)),
                                mode=getattr(pos, "mode", None),
                                pos=pos,
                                backstop_algo_id=getattr(pos, "backstop_algo_id", None),
                            )
                            log.info(
                                f"[SYNC] reconciler closed stale trade #{trade['id']} "
                                f"for {cfg.symbol} | pnl={pnl_val:.4f}"
                            )
                        except Exception as e:
                            log.warning(f"[SYNC] reconciler close failed: {e}")
                        break
            except Exception as e:
                log.debug(f"[SYNC] reconciler cleanup error: {e}")
            if getattr(pos, "backstop_algo_id", None):
                try:
                    await order_mgr._cancel_exchange_backstop(pos.backstop_algo_id)
                except Exception:
                    pass
            tracker.position = None
            tracker._clear_state()

    # Запускаем периодическую проверку состояния позиции в фоне
    check_task = asyncio.create_task(periodic_position_check())
    flat_reconcile_task = asyncio.create_task(_flat_position_reconciler())
    sim_task = asyncio.create_task(_simulate_rejected_background(client, reporter, recovery, log, shutdown_event))

    async def _watchdog():
        # Таймаут watchdog масштабируется от таймфрейма: на 1h свеча приходит
        # раз в час, поэтому лимит 15 минут убивал бота между свечами.
        #
        # По умолчанию watchdog НЕ убивает бота: при пропаже данных (WS/REST
        # недоступны) процесс остаётся жив, сам переподключается и продолжает,
        # как только свечи снова пойдут. Это избавляет от «самостопа» после
        # сетевых сбоев и позволяет оператору не перезапускать бота вручную.
        # Чтобы вернуть прежнее поведение (остановка в простое данных),
        # выставить WATCHDOG_SHUTDOWN=true.
        interval_sec = _tf_to_seconds(cfg.timeframe) or 60
        no_candle_timeout = max(interval_sec * 2.5, 900)
        shutdown_on_stale = os.getenv("WATCHDOG_SHUTDOWN", "false").lower() == "true"
        stale = False
        while not shutdown_event.is_set():
            await asyncio.sleep(60)
            if shutdown_event.is_set():
                break
            is_stale = (time.time() - last_candle_time[0]) > no_candle_timeout
            if is_stale and not stale:
                stale = True
                msg = (
                    f"[WATCHDOG] No candles processed for {int(no_candle_timeout)}s — "
                    f"data feed stale"
                )
                if shutdown_on_stale:
                    reason = "watchdog_no_candles"
                    log.error(f"{msg}, triggering shutdown (stop_reason={reason})")
                    try:
                        await reporter.report_stop_reason(reason)
                    except Exception as e:
                        log.warning(f"[WATCHDOG] failed to report stop_reason: {e}")
                    shutdown_event.set()
                    break
                log.warning(
                    f"{msg}; bot stays alive and keeps reconnecting "
                    f"(set WATCHDOG_SHUTDOWN=true to self-stop)"
                )
            elif not is_stale and stale:
                stale = False
                log.info("[WATCHDOG] candle feed resumed — bot continues")


    watchdog_task = asyncio.create_task(_watchdog())

    # Мягкая остановка (Stop в дашборде): API выставляет stop_requested, бот
    # перестаёт открывать новые позиции, доводит текущую по своей логике
    # (TP/SL/reverse) и выходит, когда станет флэт. По истечении
    # GRACEFUL_STOP_MAX_MIN минут выходит даже с открытой позицией (биржевые
    # стоп/TP остаются). Kill-кнопка по-прежнему убивает процесс жёстко.
    _stop_requested = False
    _stop_requested_since = 0.0
    _graceful_max_sec = max(0.0, float(os.getenv("GRACEFUL_STOP_MAX_MIN", "60") or 60)) * 60.0

    async def _graceful_stop_watcher():
        nonlocal _stop_requested, _stop_requested_since
        while not shutdown_event.is_set():
            await asyncio.sleep(5)
            if shutdown_event.is_set():
                break
            if not _stop_requested:
                try:
                    state = await reporter.get_bot()
                except Exception:
                    state = None
                if state and state.get("stop_requested"):
                    _stop_requested = True
                    _stop_requested_since = time.time()
                    log.info("[STOP] graceful stop requested — new entries disabled; ждём флэт")
                    try:
                        events.info("GRACEFUL_STOP_REQUESTED")
                    except Exception:
                        pass
                else:
                    continue
            if not tracker.has_open_position():
                log.info("[STOP] graceful stop: position flat — exiting")
                # Фиксируем причину: без неё карточка в дашборде после выхода
                # показывает просто «STOPPED», и оператор не видит, что это была
                # именно мягкая остановка по кнопке Stop (а не падение/автостоп).
                try:
                    await reporter.report_stop_reason("graceful_stop")
                except Exception:
                    pass
                shutdown_event.set()
                break

            if _graceful_max_sec > 0 and (time.time() - _stop_requested_since) > _graceful_max_sec:
                log.warning(
                    f"[STOP] graceful stop timeout ({int(_graceful_max_sec)}s) — "
                    f"exiting with position still open"
                )
                try:
                    await reporter.report_stop_reason("graceful_stop_timeout")
                except Exception:
                    pass
                shutdown_event.set()
                break

    graceful_task = asyncio.create_task(_graceful_stop_watcher())

    # ---- Релей testnet→live: потребление сигналов (только если включено) ----
    # LIVE получает входы и уровни стопа/тейков от testnet-бота и открывает
    # позицию СВОИМ объёмом (сайзинг live задаётся отдельно). Arm-гейт соблюдается.
    _relay_consume = os.getenv("SIGNAL_RELAY_CONSUME", "false").lower() == "true"

    async def _relay_entry(payload: dict) -> tuple:
        """Пытается открыть позицию по релейному сигналу. Возвращает (ok, note)."""
        try:
            direction = str(payload.get("direction") or "").upper()
            entry_px = float(payload.get("entry_price") or 0)
            if direction not in ("LONG", "SHORT") or entry_px <= 0:
                return False, "invalid"
            # Мягкая остановка: live-боты работают в relay-only, и это
            # ЕДИНСТВЕННЫЙ путь входа — гейт `if _stop_requested` в
            # обработчике своих сигналов для relay-only недостижим.
            if _stop_requested:
                log.info("[STOP] graceful stop requested — relay entry skipped")
                return False, "graceful_stop"
            # Защита от «просроченных» сигналов.
            created = payload.get("created_at")
            if created:
                try:
                    ts = pd.to_datetime(str(created), utc=True)
                    age = (pd.Timestamp.utcnow() - ts).total_seconds()
                    max_age = float(os.getenv("SIGNAL_RELAY_MAX_AGE_SEC", "300") or 300)
                    if max_age > 0 and age > max_age:
                        log.info(f"[RELAY] stale signal ({int(age)}s > {int(max_age)}s) — skipped")
                        return False, "stale"
                except Exception:
                    pass
            if os.getenv("REQUIRE_ARM", "false").lower() == "true" and not await _is_armed(cfg, log):
                log.info("[RELAY] not armed — signal skipped")
                return False, "not_armed"
            if tracker.has_open_position():
                log.info("[RELAY] position already open — signal skipped")
                return False, "position_open"
            tp1 = float(payload.get("tp1_price") or 0) or entry_px
            relay_signal = Signal(
                direction=direction,
                entry_price=entry_px,
                sl_price=float(payload.get("sl_price") or 0) or entry_px,
                tp1_price=tp1,
                tp2_price=float(payload.get("tp2_price") or 0) or tp1,
                timestamp=pd.Timestamp.utcnow(),
                preset=str(payload.get("preset") or "relay"),
            )
            log.info(
                f"[RELAY] executing | {direction} {cfg.symbol} preset={relay_signal.preset} "
                f"entry={entry_px} sl={relay_signal.sl_price} tp1={relay_signal.tp1_price}"
            )
            result = await order_mgr.open_position(relay_signal, mode=cfg.mode)
            if result is None:
                log.warning("[RELAY] open_position returned None")
                return False, "open_failed"
            entry_price, qty = result[0], result[1]
            await tracker.open_async(relay_signal, qty=qty)
            if tracker.position is not None:
                tracker.position.backstop_algo_id = order_mgr.backstop_algo_id
                tracker._save_state()
            events.info(
                f"RELAY_ENTRY | {direction} {cfg.symbol} preset={relay_signal.preset} "
                f"entry={entry_price} qty={qty}"
            )
            return True, "opened"
        except Exception as e:
            log.error(f"[RELAY] entry failed: {e}", exc_info=True)
            return False, f"error:{type(e).__name__}"

    async def _relay_consumer():
        while not shutdown_event.is_set():
            await asyncio.sleep(5)
            if shutdown_event.is_set():
                break
            try:
                sigs = await reporter.get_relay_signals()
            except Exception:
                sigs = []
            for s in sigs:
                ok, note = False, "error"
                try:
                    ok, note = await _relay_entry(s)
                except Exception as e:
                    log.warning(f"[RELAY] consume error: {e}")
                    note = f"error:{type(e).__name__}"
                # Ack в любом случае (consumed/skipped + причина) — для счётчиков в UI.
                try:
                    await reporter.ack_relay_signal(
                        int(s.get("id")), "consumed" if ok else "skipped", note
                    )
                except Exception:
                    pass

    relay_task = asyncio.create_task(_relay_consumer()) if _relay_consume else None

    # Последняя цена из WebSocket (markPrice) + время её получения. Используется
    # для SL/TP-тика и heartbeat, чтобы не дёргать REST ticker и не упираться в
    # лимиты. market_data.get_current_price сам решит, свежая ли WS-цена.
    ws_price: Dict[str, float] = {}

    # FIX: событие «пришла новая цена». Тик виртуального SL/TP просыпается сразу
    # на каждом обновлении (markPrice@1s/kline), а не ждёт фиксированные 5с — это
    # заметно уменьшает лаг срабатывания SL и зависимость от интервала/сети.
    price_event = asyncio.Event()

    def _on_ws_price(p: float) -> None:
        ws_price["value"] = p
        ws_price["ts"] = time.time()
        try:
            price_event.set()
        except Exception:
            pass

    async def _latest_price() -> float:
        return await get_current_price(
            client, cfg.symbol,
            ws_price=ws_price.get("value", 0.0),
            ws_ts=ws_price.get("ts", 0.0),
            logger=log,
        )

    # FIX 3: последний залогированный источник триггерной цены. Обычные тики —
    # debug, а СМЕНА источника (last→mark→rest) — info, чтобы аудит видел
    # переключения без спама на каждом тике.
    _last_trigger_source = [None]

    def _note_trigger_source(source: str, detail: str) -> None:
        prev = _last_trigger_source[0]
        if source != prev:
            log.info(
                f"[PRICE] trigger source={source} (changed from {prev or 'none'}) "
                f"| {detail}"
            )
            _last_trigger_source[0] = source
        else:
            log.debug(f"[PRICE] trigger source={source} | {detail}")

    async def _latest_trigger_price() -> tuple[float, str]:
        """FIX A: цена для виртуальных TP/SL.

        Приоритет — последняя ТОРГОВАЯ цена из kline (k.c), mark — только
        fallback при её отсутствии/устаревании (> LAST_PRICE_MAX_AGE_SEC).
        Возвращает (price, source), где source ∈ {"last","mark","rest"}.
        """
        snap = get_price_snapshot()
        now = time.time()
        last_px = float(snap.get("last_price", 0.0) or 0.0)
        last_age = now - float(snap.get("last_price_ts", 0.0) or 0.0)
        if last_px > 0 and last_age <= LAST_PRICE_MAX_AGE_SEC:
            _note_trigger_source("last", f"price={last_px} age={last_age:.2f}s")
            return last_px, "last"
        mark_px = float(ws_price.get("value", 0.0) or 0.0)
        mark_age = now - float(ws_price.get("ts", 0.0) or 0.0)
        if mark_px > 0 and mark_age <= PRICE_WS_MAX_AGE_SEC:
            _note_trigger_source(
                "mark",
                f"(last missing/stale {last_age:.2f}s) price={mark_px} "
                f"age={mark_age:.2f}s",
            )
            return mark_px, "mark"
        rest_px = await get_current_price(
            client, cfg.symbol,
            ws_price=mark_px,
            ws_ts=ws_price.get("ts", 0.0),
            logger=log,
        )
        _note_trigger_source("rest", f"price={rest_px}")
        return rest_px, "rest"

    async def _exchange_backstop_executed(pos) -> bool:
        """FIX C: True только если биржевой `botsl_*` backstop реально исполнился.

        Признак исполнения: непустой actualOrderId и статус TRIGGERED/FINISHED.
        Пустой actualOrderId или статус CANCELED/EXPIRED/NEW => не исполнялся
        (именно этот случай в ZECUSDT давал ложный REVERSE_BACKSTOP).
        """
        if cfg.mode != "live" or order_mgr is None or getattr(order_mgr, "client", None) is None:
            return False
        algo_id = getattr(pos, "backstop_algo_id", None) if pos is not None else None
        if not algo_id:
            return False
        try:
            resp = await order_mgr.client.futures_get_algo_order(algoId=int(algo_id))
        except Exception as e:
            log.debug(f"[REVERSE] backstop algo status read failed | algoId={algo_id}: {e}")
            return False
        if not isinstance(resp, dict):
            return False
        status = str(resp.get("algoStatus") or resp.get("status") or "").upper()
        actual = resp.get("actualOrderId") or resp.get("actual_order_id")
        executed = bool(actual) and status in ("TRIGGERED", "FINISHED", "FILLED")
        log.debug(
            f"[REVERSE] backstop algo check | algoId={algo_id} status={status} "
            f"actualOrderId={actual} executed={executed}"
        )
        return executed

    async def _reverse_tp_limit_filled(pos) -> bool:
        """FIX C: True, если плановый TP-лимит обратной ноги отсутствует, позиция
        уже флэт и последняя торговая цена дошла до TP (значит лимит исполнился).

        Вызывать ТОЛЬКО после того, как биржевой backstop признан неисполненным.
        """
        if cfg.mode != "live" or order_mgr is None or getattr(order_mgr, "client", None) is None:
            return False
        if pos is None:
            return False
        direction = pos.direction
        close_side = "SELL" if direction == "LONG" else "BUY"
        try:
            open_orders = await order_mgr.client.futures_get_open_orders(symbol=cfg.symbol)
        except Exception as e:
            log.debug(f"[REVERSE] TP limit presence read failed: {e}")
            return False
        for o in open_orders or []:
            if (o.get("type") or "").upper() != "LIMIT":
                continue
            if (o.get("side") or "").upper() != close_side:
                continue
            if not _is_reduce_only(o):
                continue
            return False
        try:
            real_qty = await order_mgr._get_real_position_qty(direction)
        except Exception:
            return False
        if real_qty >= DUST_QTY:
            return False
        target = pos.tp1_price or pos.tp2_price or 0.0
        px = float(get_price_snapshot().get("last_price", 0.0) or 0.0)
        if target > 0 and px > 0:
            reached = (px <= target) if direction == "SHORT" else (px >= target)
            if not reached:
                return False
        return True

    async def _find_reverse_tp_order(pos) -> Optional[dict]:
        """Открытый reduceOnly LIMIT, закрывающий обратную ногу (её TP), или None."""
        if order_mgr is None or getattr(order_mgr, "client", None) is None:
            return None
        close_side = "SELL" if pos.direction == "LONG" else "BUY"
        try:
            open_orders = await order_mgr.client.futures_get_open_orders(symbol=cfg.symbol)
        except Exception as e:
            log.debug(f"[TP_CLOSE] open orders read failed: {e}")
            return None
        for o in open_orders or []:
            if (o.get("type") or "").upper() != "LIMIT":
                continue
            if (o.get("side") or "").upper() != close_side:
                continue
            if not _is_reduce_only(o):
                continue
            return o
        return None

    def _reverse_price_beyond_tp(pos, price: float) -> bool:
        """True, если цена всё ещё за плановым TP обратной ноги (условие закрытия)."""
        target = pos.tp1_price or pos.tp2_price or 0.0
        if target <= 0 or price <= 0:
            return False
        return price <= target if pos.direction == "SHORT" else price >= target

    async def _place_marketable_reduce_limit(pos, qty: float, ref_price: float) -> bool:
        """FIX B: агрессивный reduceOnly LIMIT, пересекающий стакан (taker-филл)."""
        if order_mgr is None or getattr(order_mgr, "client", None) is None:
            return False
        close_side = "SELL" if pos.direction == "LONG" else "BUY"
        offset = float(MARKETABLE_LIMIT_OFFSET_PCT) / 100.0
        raw = ref_price * (1 + offset) if close_side == "BUY" else ref_price * (1 - offset)
        try:
            price = await order_mgr._adjust_price(raw, mode="live")
            adj_qty = await order_mgr._adjust_qty(qty, mode="live")
        except Exception as e:
            log.warning(f"[TP_CLOSE] marketable limit price/qty adjust failed: {e}")
            return False
        if price <= 0 or adj_qty <= 0:
            log.warning(
                f"[TP_CLOSE] marketable limit invalid | price={price} qty={adj_qty}"
            )
            return False
        try:
            await order_mgr.client.futures_create_order(
                symbol=cfg.symbol, side=close_side, type="LIMIT",
                price=price, quantity=adj_qty, timeInForce="GTC", reduceOnly=True,
            )
        except Exception as e:
            log.warning(
                f"[TP_CLOSE] marketable limit placement failed | side={close_side} "
                f"price={price} qty={adj_qty}: {e}"
            )
            return False
        try:
            order_mgr._invalidate_caches()
        except Exception:
            pass
        log.info(
            f"[TP_CLOSE] replaced with marketable limit | price={price} "
            f"qty={adj_qty} side={close_side} ref={ref_price}"
        )
        return True

    async def _ensure_reverse_tp_fill(pos) -> tuple[Optional[str], float]:
        """FIX B: не даёт виртуальному TP выбить рабочий лимит по рынку.

        Дерево решений:
          1. resting TP-лимит FILLED -> ("tp", price);
          2. NEW/PARTIALLY_FILLED  -> grace-poll до REVERSE_TP_GRACE_SEC:
               - филл -> ("tp", price);
               - цена вернулась внутрь TP -> (None, 0.0) — позицию оставляем;
          3. после grace цена всё ещё за TP и лимит не исполнен -> CANCEL лимита
             и агрессивный marketable LIMIT:
               - филл -> ("market", price);
          4. marketable не сработал -> рыночное закрытие -> ("market", price).
        Возвращает (None, 0.0), только если закрывать не нужно.
        """
        direction = pos.direction
        target_px = pos.tp1_price or pos.tp2_price or 0.0
        _deadline_key = (
            cfg.symbol, str(direction),
            float(getattr(pos, "entry_price", 0.0) or 0.0),
            int(getattr(pos, "entry_fill_ms", 0) or 0),
        )
        # Paper/testnet без биржи: виртуальный TP сам является моделью филла.
        if cfg.mode != "live" or order_mgr is None or getattr(order_mgr, "client", None) is None:
            return "tp", target_px

        last_px, _src = await _latest_trigger_price()
        order = await _find_reverse_tp_order(pos)
        if order is not None:
            status = str(order.get("status") or "").upper()
            order_id = order.get("orderId")
            if status == "FILLED":
                return "tp", last_px
            if status in ("NEW", "PARTIALLY_FILLED"):
                # Не блокируем tick-цикл: филл лимита ждём по тикам, а не
                # sleep-loop'ом. Дедлайн переживает вызовы; FILLED/флэт ловится
                # в начале функции на следующем тике.
                now = time.time()
                deadline = _REVERSE_TP_DEADLINES.get(_deadline_key, 0.0)
                if deadline <= 0:
                    _REVERSE_TP_DEADLINES[_deadline_key] = now + REVERSE_TP_GRACE_SEC
                    log.info(
                        f"[TP_CLOSE] waiting for limit fill | status={status} "
                        f"orderId={order_id} tp={target_px} last={last_px}"
                    )
                    return None, 0.0
                if not _reverse_price_beyond_tp(pos, last_px):
                    log.info(
                        f"[TP_CLOSE] price returned inside TP band | last={last_px} "
                        f"tp={target_px} — keeping resting limit"
                    )
                    _REVERSE_TP_DEADLINES.pop(_deadline_key, None)
                    return None, 0.0
                if now < deadline:
                    return None, 0.0
                _REVERSE_TP_DEADLINES.pop(_deadline_key, None)
                log.info(
                    f"[TP_CLOSE] waiting for limit fill timed out | tp={target_px} "
                    f"last={last_px}"
                )
                try:
                    await order_mgr.client.futures_cancel_order(
                        symbol=cfg.symbol, orderId=order_id
                    )
                except Exception as e:
                    log.warning(f"[TP_CLOSE] cancel resting TP failed: {e}")
        else:
            # Лимита нет: либо уже исполнен (позиция флэт), либо снят/пропал.
            try:
                order_mgr._invalidate_position_cache()
                real_qty = await order_mgr._get_real_position_qty(direction)
            except Exception:
                real_qty = -1.0
            if 0.0 <= real_qty < DUST_QTY:
                return "tp", last_px
            # Лимита нет, а позиция ещё открыта: переставляем TP-лимит вместо
            # агрессивного закрытия по рынку (снижает долю REVERSE_MARKET).
            if real_qty >= DUST_QTY and target_px and target_px > 0:
                try:
                    await order_mgr._place_tp_limit(direction, target_px, real_qty)
                    _REVERSE_TP_DEADLINES[_deadline_key] = time.time() + REVERSE_TP_GRACE_SEC
                    log.info(
                        f"[TP_CLOSE] resting TP limit missing — re-placed | dir={direction} "
                        f"qty={real_qty} tp={target_px} last={last_px} "
                        f"beyond_tp={_reverse_price_beyond_tp(pos, last_px)}"
                    )
                    return None, 0.0
                except Exception as e:
                    log.warning(f"[TP_CLOSE] re-place TP failed: {e}")
            log.info(
                f"[TP_CLOSE] resting TP limit missing | dir={direction} "
                f"real_qty={real_qty} tp={target_px} — going aggressive"
            )

        # 3. агрессивный marketable limit
        try:
            order_mgr._invalidate_position_cache()
            real_qty = await order_mgr._get_real_position_qty(direction)
        except Exception:
            real_qty = -1.0
        if real_qty < 0:
            real_qty = pos.remaining_qty
        if real_qty >= DUST_QTY:
            if await _place_marketable_reduce_limit(pos, real_qty, last_px):
                for _ in range(int(REVERSE_TP_GRACE_SEC)):
                    if shutdown_event.is_set():
                        break
                    await asyncio.sleep(1.0)
                    try:
                        order_mgr._invalidate_position_cache()
                        q = await order_mgr._get_real_position_qty(direction)
                    except Exception:
                        q = -1.0
                    if 0.0 <= q < DUST_QTY:
                        log.info(
                            f"[TP_CLOSE] marketable limit filled | dir={direction} qty={q}"
                        )
                        return "market", last_px

        # 4. последний рубеж — market close
        log.warning(
            f"[TP_CLOSE] force market close | dir={direction} qty={real_qty} "
            f"last={last_px} tp={target_px}"
        )
        try:
            await order_mgr.cancel_all_tp_sl(direction, mode=cfg.mode)
            closed = await order_mgr.close_position_market(direction, mode=cfg.mode)
            if not closed:
                closed = await order_mgr.close_dust(direction, mode=cfg.mode)
        except Exception as e:
            log.error(f"[TP_CLOSE] force market close failed: {e}")
        return "market", last_px

    async def _heartbeat_task():
        # Регулярный heartbeat между свечами: на 1h без него дашборд показывает
        # устаревший last_heartbeat почти час. Обновляем каждые 60 сек.
        while not shutdown_event.is_set():
            await asyncio.sleep(60)
            if shutdown_event.is_set():
                break
            current_price = await _latest_price()
            if current_price <= 0:
                continue
            await reporter.report_heartbeat(current_price)

    heartbeat_task = asyncio.create_task(_heartbeat_task())

    last_tick_hb_ts = 0.0

    async def tick_sl_tp_check():
        nonlocal last_tick_hb_ts
        while not shutdown_event.is_set():
            try:
                # Просыпаемся на каждое обновление цены (price_event) либо минимум
                # раз в 1с: виртуальный SL/TP срабатывает максимально быстро и не
                # зависит от 5-секундного интервала.
                try:
                    await asyncio.wait_for(price_event.wait(), timeout=1.0)
                except asyncio.TimeoutError:
                    pass
                finally:
                    price_event.clear()
                if shutdown_event.is_set():
                    break
                if not tracker.has_open_position():
                    continue
                # FIX A: виртуальный TP/SL считается по последней торговой цене,
                # mark — только fallback (см. _latest_trigger_price).
                current_price, _price_src = await _latest_trigger_price()
                if current_price <= 0:
                    continue
                # Обновляем live-цену для дашборда, чтобы unrealized PnL в карточке
                # бота пересчитывался между свечами, а не залипал на цене открытия.
                # Защита: никогда не перезаписываем цену нулём (Binance может вернуть 0
                # при лимите/бане — это породит ложный unrealized PnL в дашборде).
                now_ts = time.time()
                if reporter is not None and current_price > 0 and (now_ts - last_tick_hb_ts) >= 30:
                    last_tick_hb_ts = now_ts
                    await reporter.report_heartbeat(current_price)
                hit = tracker.check(current_price)
                if hit:
                    candle_time_ms = int(time.time() * 1000)
                    await process_hit(hit, current_price, candle_time_ms)
                    if not tracker.has_open_position() and cfg.symbol in _reverse_cum_loss:
                        _reverse_cum_loss.pop(cfg.symbol, None)
                        _reverse_cum_ts.pop(cfg.symbol, None)
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
                current_price = await _latest_price()
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
                if not tracker.has_open_position() and cfg.symbol in _reverse_cum_loss:
                    _reverse_cum_loss.pop(cfg.symbol, None)
                    _reverse_cum_ts.pop(cfg.symbol, None)
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
                    await notifier.send_message(
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

    use_ws = os.getenv("USE_WEBSOCKET", "true").lower() == "true"
    if use_ws:
        await start_kline_websocket(
            client=client, symbol=cfg.symbol, handlers=handlers,
            logger=log, shutdown_event=shutdown_event,
            on_price=lambda p: _on_ws_price(p),
        )
    else:
        await start_kline_polling(
            client=client, symbol=cfg.symbol, handlers=handlers,
            logger=log, poll_seconds=60, shutdown_event=shutdown_event,
        )
    
    # Останавливаем фоновые задачи.
    # ВАЖНО: не отменяем tick_task мгновенно — он может быть в середине
    # финализации сделки (tick_sl_tp_check → process_hit → _report_close_with_id).
    # Мгновенный cancel() теряет close-PATCH, и строка trades навсегда остаётся
    # is_open=1 («фантомная открытая сделка»). shutdown_event уже взведён, и
    # tick_task выйдет сам на ближайшей проверке (~1с); даём ему дописать
    # закрытие, затем отменяем всё остальное.
    _finalize_grace_sec = float(os.getenv("SHUTDOWN_FINALIZE_GRACE_SEC") or "20")
    try:
        await asyncio.wait_for(tick_task, timeout=_finalize_grace_sec)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass

    _cancel_tasks = [check_task, flat_reconcile_task, sim_task, watchdog_task, time_profit_task, tick_task, heartbeat_task, graceful_task]
    if relay_task is not None:
        _cancel_tasks.append(relay_task)
    for task in _cancel_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


if __name__ == "__main__":
    asyncio.run(main())

