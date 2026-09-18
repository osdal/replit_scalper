import json
import os
import time
import datetime
import asyncio
from dataclasses import dataclass, field
from typing import Optional, TYPE_CHECKING

from notifier import Notifier
import logging

from strategy import Signal
from config import Config

if TYPE_CHECKING:
    from db_reporter import DbReporter
    from order_manager import OrderManager

STATE_FILE_TEMPLATE = "state_{symbol}.json"


def _state_file(symbol: str) -> str:
    return STATE_FILE_TEMPLATE.replace("{symbol}", symbol.lower())


def _to_epoch_ms(value) -> int:
    """Конвертирует datetime/ISO-строку/epoch в миллисекунды эпохи.

    Naive-времена трактуются как UTC (а не как локальное время), поэтому
    окно Binance userTrades не смещается на часовой пояс хоста. Принимает
    trailing 'Z'. Возвращает 0, если значение распарсить не удалось.
    """
    if value is None:
        return 0
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        v = float(value)
        return int(v * 1000) if v < 1e12 else int(v)
    dt = None
    if isinstance(value, datetime.datetime):
        dt = value
    elif isinstance(value, str):
        s = value.strip()
        if not s:
            return 0
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(s)
        except ValueError:
            for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
                try:
                    dt = datetime.datetime.strptime(s.replace("T", " "), fmt)
                    break
                except ValueError:
                    continue
    if dt is None:
        return 0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    else:
        dt = dt.astimezone(datetime.timezone.utc)
    return int(dt.timestamp() * 1000)


@dataclass
class Position:
    direction: str
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    total_qty: float
    remaining_qty: float
    tp1_hit: bool = False
    closed: bool = False
    realized_pnl: float = 0.0
    entry_timestamp: Optional[object] = None
    entry_ema_fast: float = 0.0
    entry_ema_slow: float = 0.0
    entry_volume: float = 0.0
    entry_volume_ma: float = 0.0
    entry_rsi: float = 0.0
    entry_macd_hist: float = 0.0
    entry_bb_lower: float = 0.0
    entry_bb_upper: float = 0.0
    entry_atr: float = 0.0
    preset: str = "ema_cross"
    is_recovery: bool = False       # True если это компенсирующая сделка
    recovery_chain_id: Optional[int] = None
    opened_at: Optional[str] = None  # ISO timestamp when position was opened (for TIME_PROFIT_CLOSE_HOURS)
    mode: Optional[str] = None       # "paper"|"live"|None — режим сделки (из пресета)
    reject_reason: Optional[str] = None  # если задан — сделка помечена как REJECTED (не в статистике)
    regime_adx: float = 0.0          # ADX на момент входа (только бэктест, не сериализуется)
    regime_atr_pct: float = 0.0      # ATR% на момент входа (только бэктест)
    is_reverse: bool = False         # True если позиция открыта после срабатывания SL (обратная)
    reversed_from_pnl: float = 0.0   # PnL исходной позиции на момент открытия reverse
    reversed_from_direction: str = ""  # направление исходной позиции
    reversed_from_qty: float = 0.0   # объём исходной позиции
    reversed_from_entry: float = 0.0 # цена входа исходной позиции
    regime_trend: str = ""           # наклон EMA на входе: "LONG"/"SHORT" (только бэктест)
    intrabar_return: float = 0.0     # движение внутри свечи входа (только бэктест)
    voting_bases: list = field(default_factory=list)  # согласовавшие стратегии (только бэктест)
    backstop_algo_id: Optional[int] = None  # algoId биржевого safety-net STOP_MARKET (closePosition)
    entry_fill_ms: Optional[int] = None     # фактическое время входа (мс, UTC) с биржи — начало окна цикла

    def unrealized_pnl(self, current_price: float) -> float:
        if self.direction == "LONG":
            return (current_price - self.entry_price) * self.remaining_qty
        else:
            return (self.entry_price - current_price) * self.remaining_qty


class PositionTracker:
    def __init__(self, cfg: Config, logger: logging.Logger, reporter: Optional["DbReporter"] = None, order_mgr: Optional["OrderManager"] = None, notifier: Optional["Notifier"] = None):
        self.cfg = cfg
        self.log = logger
        self.reporter = reporter
        self.order_mgr = order_mgr
        self.notifier = notifier
        self.position: Optional[Position] = None
        self._state_file = _state_file(cfg.symbol)
        self._trade_id: Optional[int] = None  # ID сделки в БД
        # Фактическое время входа (мс, UTC) текущего цикла. Переживает очистку
        # self.position, чтобы close-репортинг использовал его как начало окна.
        self._entry_fill_ms: Optional[int] = None

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def _save_state(self) -> None:
        if self.position is None:
            self._clear_state()
            return
        p = self.position
        data = {
            "direction":     p.direction,
            "entry_price":   p.entry_price,
            "sl_price":      p.sl_price,
            "tp1_price":     p.tp1_price,
            "tp2_price":     p.tp2_price,
            "total_qty":     p.total_qty,
            "remaining_qty": p.remaining_qty,
            "tp1_hit":       p.tp1_hit,
            "realized_pnl":  p.realized_pnl,
            "entry_timestamp": str(p.entry_timestamp) if p.entry_timestamp else None,
            "entry_ema_fast":  p.entry_ema_fast,
            "entry_ema_slow":  p.entry_ema_slow,
            "entry_volume":    p.entry_volume,
            "entry_volume_ma": p.entry_volume_ma,
            "entry_rsi": p.entry_rsi,
            "entry_macd_hist": p.entry_macd_hist,
            "entry_bb_lower": p.entry_bb_lower,
            "entry_bb_upper": p.entry_bb_upper,
            "entry_atr": p.entry_atr,
            "preset": p.preset,
            "is_recovery":     p.is_recovery,
            "recovery_chain_id": p.recovery_chain_id,
            "trade_id":        self._trade_id,
            "opened_at":       p.opened_at,
            "mode":            p.mode,
            "reject_reason":   p.reject_reason,
            "backstop_algo_id": p.backstop_algo_id,
            "entry_fill_ms":   p.entry_fill_ms,
        }
        try:
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log.error(f"[STATE] Failed to save state: {e}")

    def _clear_state(self) -> None:
        try:
            if os.path.exists(self._state_file):
                os.remove(self._state_file)
        except Exception as e:
            self.log.error(f"[STATE] Failed to clear state: {e}")

    def load_state(self) -> bool:
        if not os.path.exists(self._state_file):
            return False
        try:
            with open(self._state_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.position = Position(
                direction=data["direction"],
                entry_price=data["entry_price"],
                sl_price=data["sl_price"],
                tp1_price=data["tp1_price"],
                tp2_price=data["tp2_price"],
                total_qty=data["total_qty"],
                remaining_qty=data["remaining_qty"],
                tp1_hit=data.get("tp1_hit", False),
                realized_pnl=data.get("realized_pnl", 0.0),
                entry_timestamp=data.get("entry_timestamp"),
                entry_ema_fast=data.get("entry_ema_fast", 0.0),
                entry_ema_slow=data.get("entry_ema_slow", 0.0),
                entry_volume=data.get("entry_volume", 0.0),
                entry_volume_ma=data.get("entry_volume_ma", 0.0),
                entry_rsi=data.get("entry_rsi", 0.0),
                entry_macd_hist=data.get("entry_macd_hist", 0.0),
                entry_bb_lower=data.get("entry_bb_lower", 0.0),
                entry_bb_upper=data.get("entry_bb_upper", 0.0),
                entry_atr=data.get("entry_atr", 0.0),
                preset=data.get("preset", "ema_cross"),
                is_recovery=data.get("is_recovery", False),
                recovery_chain_id=data.get("recovery_chain_id"),
                opened_at=data.get("opened_at"),
                mode=data.get("mode"),
                reject_reason=data.get("reject_reason"),
                backstop_algo_id=data.get("backstop_algo_id"),
                entry_fill_ms=data.get("entry_fill_ms"),
            )
            self._trade_id = data.get("trade_id")
            self._entry_fill_ms = self.position.entry_fill_ms
            self.log.info(
                f"[STATE] Restored from file | {self.position.direction} "
                f"entry={self.position.entry_price} "
                f"SL={self.position.sl_price} "
                f"TP1={self.position.tp1_price} "
                f"TP2={self.position.tp2_price} "
                f"qty={self.position.remaining_qty} "
                f"tp1_hit={self.position.tp1_hit}"
            )
            return True
        except Exception as e:
            self.log.error(f"[STATE] Failed to load state: {e}")
            return False

    # ------------------------------------------------------------------ #
    #  Reporter helpers                                                    #
    # ------------------------------------------------------------------ #

    async def _report_open(self, signal: Signal, qty: float, reject_reason: Optional[str] = None) -> None:
        if not self.reporter:
            return
        try:
            trade_data = {
                "symbol":      self.cfg.symbol,
                "direction":   signal.direction,
                "entry_price": signal.entry_price,
                "sl_price":    signal.sl_price,
                "tp1_price":   signal.tp1_price,
                "tp2_price":   signal.tp2_price,
                "qty":         qty,
                "entry_time":  str(signal.timestamp).replace(" ", "T"),
                "is_open":     True,
                "mode":        signal.mode or self.cfg.mode,
                "ema_fast":    signal.ema_fast,
                "ema_slow":    signal.ema_slow,
                "volume":      signal.volume,
                "volume_ma":   signal.volume_ma,
                "rsi":         signal.rsi,
                "macd":        signal.macd,
                "macd_signal": signal.macd_signal,
                "macd_hist":   signal.macd_hist,
                "bb_upper":    signal.bb_upper,
                "bb_middle":   signal.bb_middle,
                "bb_lower":    signal.bb_lower,
                "atr":         signal.atr,
                "preset":      signal.preset,
            }
            if reject_reason:
                # Отклонённая (но открытая) позиция: помечаем, чтобы исключить из статистики.
                trade_data["status"] = "rejected"
                trade_data["reject_reason"] = reject_reason
            trade_id = await self.reporter.report_trade(trade_data)
            if trade_id:
                self._trade_id = trade_id
        except Exception as e:
            self.log.debug(f"[REPORTER] report_open error: {e}")

    async def update_open_trade(self, direction: str, entry_price: float, qty: float) -> bool:
        """Обновляет открытую запись trades под текущую ногу цикла.

        Используется при reverse: цикл репортится на той же строке БД, что и
        исходная нога, поэтому при развороте строка должна отражать живую ногу
        (direction/entry_price/qty), сохраняя id, is_open и status. TP/SL
        оставляем как есть — их перезапишет закрытие по REVERSE.
        """
        if not self.reporter or not self._trade_id:
            return False
        try:
            success = await self.reporter.patch_trade(self._trade_id, {
                "direction":   direction,
                "entry_price": entry_price,
                "qty":         qty,
            })
            if not success:
                self.log.warning(
                    f"[REPORTER] update_open_trade failed for trade #{self._trade_id}"
                )
            return success
        except Exception as e:
            self.log.debug(f"[REPORTER] update_open_trade error: {e}")
            return False

    async def _entry_time_ms(self, trade_id: Optional[int], prefer_db: bool = False) -> int:
        """Возвращает время входа позиции/цикла в мс — из самого объекта, либо из
        записи в БД. Все naive-времена трактуются как UTC. Для reverse-цикла
        (prefer_db=True) берётся entry_time исходной ноги из БД, чтобы окно
        покрывало весь цикл. Если время отсутствует или находится в будущем —
        fallback now-24h (с предупреждением)."""
        now_ms = int(time.time() * 1000)
        # Приоритет — фактическое время входа цикла с биржи (entry_fill_ms).
        # Кэш переживает очистку self.position при закрытии.
        cached_fill_ms = self._entry_fill_ms
        if not cached_fill_ms and self.position is not None:
            cached_fill_ms = getattr(self.position, "entry_fill_ms", None)
        if cached_fill_ms and cached_fill_ms > 0:
            return int(cached_fill_ms)
        pos_ts = getattr(self.position, "entry_timestamp", None) if self.position else None
        entry_ms = 0
        if pos_ts and not prefer_db:
            entry_ms = _to_epoch_ms(pos_ts)
        # Fallback: entry_time из БД по trade_id
        if entry_ms <= 0 and trade_id and self.reporter:
            try:
                rec = await self.reporter.get_trade(trade_id)
                if rec and rec.get("entry_time"):
                    entry_ms = _to_epoch_ms(rec["entry_time"])
            except Exception:
                pass
        if entry_ms <= 0 and pos_ts:
            entry_ms = _to_epoch_ms(pos_ts)
        if entry_ms <= 0 or entry_ms > now_ms:
            fallback_ms = now_ms - 24 * 60 * 60 * 1000
            self.log.warning(
                f"[TIME] Invalid entry time (entry_ms={entry_ms}) for trade {trade_id}; "
                f"falling back to now-24h ({fallback_ms})"
            )
            return fallback_ms
        return entry_ms

    async def _report_close(self, exit_price: float, qty: float, pnl: float, reason: str, entry_price: float = 0.0, reject_reason: Optional[str] = None, commission: Optional[float] = None) -> None:
        if not self.reporter or not self._trade_id:
            return
        try:
            if commission is not None:
                # Реальные значения с биржи: PnL уже net, комиссия — фактическая.
                commission_to_use = commission
                pnl_to_use = pnl
            else:
                commission_to_use, pnl_to_use = self._apply_commission(entry_price, exit_price, qty, pnl)
                # Try to get real PnL from Binance to match position history
                real_pnl = None
                if self.order_mgr and not reject_reason:
                    entry_time_ms = await self._entry_time_ms(self._trade_id)
                    if entry_time_ms > 0:
                        try:
                            exit_time_ms = int(time.time() * 1000)
                            real_pnl = await self.order_mgr.get_realized_pnl(
                                self.cfg.symbol, entry_time_ms, exit_time_ms,
                            )
                        except Exception:
                            pass
                if real_pnl is not None and abs(real_pnl) > 0.0001:
                    pnl_to_use = real_pnl
                    # В live-режиме комиссия уже учтена в реальном PnL, показываем её как расчётную.
                    commission_to_use = self._estimated_commission(entry_price, exit_price, qty)
            status = "rejected" if reject_reason else "closed"
            success = await self.reporter.patch_trade(self._trade_id, {
                "exit_price":  exit_price,
                "qty":         qty,
                "pnl":         pnl_to_use,
                "commission":  commission_to_use,
                "exit_reason": reason,
                "exit_time":   datetime.datetime.utcnow().isoformat(),
                "is_open":     False,
                "status":      status,
            })
            if not success:
                # Запись не найдена (например после очистки БД) — создаём новую
                self.log.warning(f"[REPORTER] trade #{self._trade_id} not found, creating new record")
                p = self.position
                new_trade = {
                    "symbol":      self.cfg.symbol,
                    "direction":   p.direction if p else "LONG",
                    "entry_price": p.entry_price if p else (entry_price or exit_price),
                    "exit_price":  exit_price,
                    "qty":         qty,
                    "pnl":         pnl_to_use,
                    "commission":  commission_to_use,
                    "exit_reason": reason,
                    "entry_time":  str(p.entry_timestamp).replace(" ", "T") if p and p.entry_timestamp else datetime.datetime.utcnow().isoformat(),
                    "exit_time":   datetime.datetime.utcnow().isoformat(),
                    "is_open":     False,
                    "status":      status,
                    "mode":        self.cfg.mode,
                }
                if reject_reason:
                    new_trade["reject_reason"] = reject_reason
                await self.reporter.report_trade(new_trade)
            self._trade_id = None
        except Exception as e:
            self.log.debug(f"[REPORTER] report_close error: {e}")

    async def _report_close_with_id(self, trade_id: int, exit_price: float, qty: float, pnl: float, reason: str, entry_price: float = 0.0, reject_reason: Optional[str] = None, commission: Optional[float] = None) -> None:
        """Закрывает сделку по указанному trade_id (используется после _clear_state)."""
        if not self.reporter:
            return
        if trade_id is None:
            self.log.warning("[REPORTER] _report_close_with_id called without trade_id; skip")
            return
        try:
            if commission is not None:
                # Реальные значения с биржи: PnL уже net, комиссия — фактическая.
                commission_to_use = commission
                pnl_to_use = pnl
            else:
                commission_to_use, pnl_to_use = self._apply_commission(entry_price, exit_price, qty, pnl)
                # Try to get real PnL from Binance to match position history
                real_pnl = None
                if self.order_mgr and not reject_reason:
                    entry_time_ms = await self._entry_time_ms(trade_id)
                    if entry_time_ms > 0:
                        try:
                            exit_time_ms = int(time.time() * 1000)
                            real_pnl = await self.order_mgr.get_realized_pnl(
                                self.cfg.symbol, entry_time_ms, exit_time_ms,
                            )
                        except Exception:
                            pass
                if real_pnl is not None and abs(real_pnl) > 0.0001:
                    pnl_to_use = real_pnl
                    commission_to_use = self._estimated_commission(entry_price, exit_price, qty)
            status = "rejected" if reject_reason else "closed"
            success = await self.reporter.patch_trade(trade_id, {
                "exit_price":  exit_price,
                "qty":         qty,
                "pnl":         pnl_to_use,
                "commission":  commission_to_use,
                "exit_reason": reason,
                "exit_time":   datetime.datetime.utcnow().isoformat(),
                "is_open":     False,
                "status":      status,
            })
            if not success:
                # Запись не найдена (например после очистки БД) — создаём новую
                self.log.warning(f"[REPORTER] trade #{trade_id} not found, creating new record")
                p = self.position
                new_trade = {
                    "symbol":      self.cfg.symbol,
                    "direction":   p.direction if p else "LONG",
                    "entry_price": p.entry_price if p else (entry_price or exit_price),
                    "exit_price":  exit_price,
                    "qty":         qty,
                    "pnl":         pnl_to_use,
                    "commission":  commission_to_use,
                    "exit_reason": reason,
                    "entry_time":  str(p.entry_timestamp).replace(" ", "T") if p and p.entry_timestamp else datetime.datetime.utcnow().isoformat(),
                    "exit_time":   datetime.datetime.utcnow().isoformat(),
                    "is_open":     False,
                    "status":      status,
                    "mode":        self.cfg.mode,
                }
                if reject_reason:
                    new_trade["reject_reason"] = reject_reason
                await self.reporter.report_trade(new_trade)
        except Exception as e:
            self.log.debug(f"[REPORTER] report_close_with_id error: {e}")

    def _estimated_commission(self, entry_price: float, exit_price: float, qty: float) -> float:
        """Расчётная (симулируемая) комиссия Taker для сделки в USDT."""
        eff_mode = (self.position.mode if self.position else None) or self.cfg.mode
        if eff_mode == "live":
            return 0.0  # В live комиссия берётся с биржи, в БД не пишем расчётную
        fee = self.cfg.commission_pct / 100.0
        return round((abs(entry_price) + abs(exit_price)) * abs(qty) * fee, 8)

    def _apply_commission(self, entry_price: float, exit_price: float, qty: float, pnl: float) -> tuple[float, float]:
        """Возвращает (commission, net_pnl): вычитает симулируемую комиссию из PnL."""
        commission = self._estimated_commission(entry_price, exit_price, qty)
        return commission, pnl - commission

    async def _report_tp1(self, exit_price: float, qty: float, pnl: float) -> None:
        """TP1 — частичное закрытие. НЕ записываем в БД, только обновляем состояние."""
        # Не репортим TP1 в БД — ждём полного закрытия позиции
        # Состояние обновляется через apply_hit -> _save_state()
        pass

    # ------------------------------------------------------------------ #
    #  Entry-fill capture (окно цикла)                                     #
    # ------------------------------------------------------------------ #

    def _price_tol(self, reference: float) -> float:
        """Допуск сравнения цен: один тик символа либо небольшая (0.01%)
        относительная погрешность, если тик недоступен."""
        tick = getattr(self.order_mgr, "_tick_size", None) if self.order_mgr else None
        return max(float(tick) if tick else 0.0, abs(reference) * 1e-4, 1e-9)

    def _price_close(self, a: float, b: float) -> bool:
        if a <= 0 or b <= 0:
            return False
        return abs(a - b) <= self._price_tol(b)

    @classmethod
    def _match_entry_fill_ms(
        cls, trades: Optional[list], direction: str, entry_price: float,
        entry_qty: float, tol: float,
    ) -> int:
        """Первый (самый ранний) филл входа по стороне/цене/объёму.

        Используется как fallback, когда entry_fill_ms не удалось зафиксировать
        при открытии. Возвращает мс или 0.
        """
        want_side = "BUY" if direction == "LONG" else "SELL"
        best = 0
        for t in trades or []:
            try:
                if (t.get("side") or "") != want_side:
                    continue
                price = float(t.get("price", 0) or 0)
                if price <= 0 or abs(price - entry_price) > tol:
                    continue
                fqty = float(t.get("qty", 0) or 0)
                # Допускаем дробление входа на несколько филлов, но отсекаем
                # чужие (крупнее) сделки.
                if entry_qty > 0 and fqty > entry_qty * 1.5 + 1e-9:
                    continue
                ts = int(t.get("time", 0) or 0)
                if ts > 0 and (best == 0 or ts < best):
                    best = ts
            except (TypeError, ValueError):
                continue
        return best

    async def _capture_entry_fill_ms(self) -> None:
        """Фиксирует фактическое время входа по userTrades биржи и сохраняет его
        в позиции/стейте. Кандл-open entry_time ~на 5 мин раньше реального
        входа, из-за чего в окно цикла затягивались филлы предыдущего цикла."""
        p = self.position
        if p is None or p.entry_fill_ms or p.reject_reason:
            return
        # Фиксируем только реальный биржевой вход: в paper/backtest филлов нет.
        if (p.mode or self.cfg.mode) != "live":
            return
        if not self.order_mgr or not getattr(self.order_mgr, "client", None):
            return
        now_ms = int(time.time() * 1000)
        start_ms = now_ms - 5 * 60 * 1000
        try:
            trades = await asyncio.wait_for(
                self.order_mgr.client.futures_account_trades(
                    symbol=self.cfg.symbol,
                    startTime=start_ms,
                    endTime=now_ms,
                ),
                timeout=15,
            )
        except Exception as e:
            self.log.debug(f"[ENTRY_FILL] capture failed for {self.cfg.symbol}: {e}")
            return
        tol = self._price_tol(p.entry_price)
        fill_ms = self._match_entry_fill_ms(trades, p.direction, p.entry_price, p.total_qty, tol)
        if fill_ms > 0:
            p.entry_fill_ms = fill_ms
            self._entry_fill_ms = fill_ms
            self.log.info(
                f"[ENTRY_FILL] trade_id={self._trade_id} entry_fill_ms={fill_ms} "
                f"entry={p.entry_price} qty={p.total_qty} dir={p.direction}"
            )
        else:
            self.log.warning(
                f"[ENTRY_FILL] entry fill not found via userTrades for trade "
                f"{self._trade_id}; falling back to candle entry_time"
            )

    def _classify_reverse_exit(self, p_before: Optional[Position], exit_price: Optional[float], hit: str) -> str:
        """REVERSE_TP, если обратная нога вышла на плановом TP (P3), иначе
        REVERSE_SL (биржевой backstop или любой другой рыночный выход).

        Сравниваем фактическую цену выхода с запланированными tp1/tp2 обратной
        позиции с допуском в один тик / небольшую относительную погрешность.
        """
        if exit_price is None or exit_price <= 0:
            # Цена выхода недоступна: planned-TP хит обратной ноги трактуем как TP.
            return "REVERSE_TP" if hit == "TP1" else "REVERSE_SL"
        targets = [
            getattr(p_before, "tp1_price", 0.0) if p_before else 0.0,
            getattr(p_before, "tp2_price", 0.0) if p_before else 0.0,
        ]
        for target in targets:
            if target and target > 0 and self._price_close(exit_price, target):
                return "REVERSE_TP"
        return "REVERSE_SL"

    # ------------------------------------------------------------------ #
    #  Trading logic                                                       #
    # ------------------------------------------------------------------ #

    def open(
        self, signal: Signal, qty: float,
        is_recovery: bool = False, recovery_chain_id: Optional[int] = None,
        reject_reason: Optional[str] = None,
        is_reverse: bool = False,
        reversed_from_pnl: float = 0.0,
        reversed_from_direction: str = "",
        reversed_from_qty: float = 0.0,
        reversed_from_entry: float = 0.0,
    ) -> None:
        # Reverse — продолжение того же цикла/строки БД, поэтому фактическое
        # время входа исходной ноги переносится в обратную ногу: окно закрытия
        # должно начинаться со входа цикла, а не с разворота.
        carried_fill_ms = (
            getattr(self.position, "entry_fill_ms", None)
            if (is_reverse and self.position is not None) else None
        )
        self.position = Position(
            direction=signal.direction,
            entry_price=signal.entry_price,
            sl_price=signal.sl_price,
            tp1_price=signal.tp1_price,
            tp2_price=signal.tp2_price,
            total_qty=qty,
            remaining_qty=qty,
            entry_timestamp=signal.timestamp,
            entry_ema_fast=signal.ema_fast,
            entry_ema_slow=signal.ema_slow,
            entry_volume=signal.volume,
            entry_volume_ma=signal.volume_ma,
            entry_rsi=signal.rsi,
            entry_macd_hist=signal.macd_hist,
            entry_bb_lower=signal.bb_lower,
            entry_bb_upper=signal.bb_upper,
            entry_atr=signal.atr,
            preset=signal.preset,
            is_recovery=is_recovery,
            recovery_chain_id=recovery_chain_id,
            opened_at=datetime.datetime.utcnow().isoformat() if signal.timestamp is None else str(signal.timestamp).replace(" ", "T"),
            mode=signal.mode,
            reject_reason=reject_reason,
            voting_bases=list(getattr(signal, "voting_bases", []) or []),
            is_reverse=is_reverse,
            reversed_from_pnl=reversed_from_pnl,
            reversed_from_direction=reversed_from_direction,
            reversed_from_qty=reversed_from_qty,
            reversed_from_entry=reversed_from_entry,
            entry_fill_ms=carried_fill_ms,
        )
        self._trade_id = None
        self._entry_fill_ms = carried_fill_ms
        self._save_state()
        tag = " [RECOVERY]" if is_recovery else ""
        tag += " [REJECTED]" if reject_reason else ""
        self.log.info(
            f"Position opened{tag} | {signal.direction} | entry={signal.entry_price} "
            f"SL={signal.sl_price} TP1={signal.tp1_price} TP2={signal.tp2_price} qty={qty} | "
            f"indicators: ema_fast={signal.ema_fast} ema_slow={signal.ema_slow} "
            f"volume={signal.volume} volume_ma={signal.volume_ma} "
            f"rsi={signal.rsi:.1f} macd_hist={signal.macd_hist:.6f} "
            f"bb=[{signal.bb_lower:.4f}..{signal.bb_upper:.4f}] atr={signal.atr:.6f}"
        )

    async def open_async(
        self, signal: Signal, qty: float,
        is_recovery: bool = False, recovery_chain_id: Optional[int] = None,
        reject_reason: Optional[str] = None,
    ) -> None:
        """Открывает позицию и репортит в БД."""
        self.open(signal, qty, is_recovery=is_recovery, recovery_chain_id=recovery_chain_id, reject_reason=reject_reason)
        await self._report_open(signal, qty, reject_reason=reject_reason)
        await self._capture_entry_fill_ms()
        self._save_state()

    def force_close(self, reason: str, close_price: float) -> float:
        p = self.position
        if p is None:
            return 0.0
        qty = p.remaining_qty
        pnl = self._calc_pnl(p.direction, p.entry_price, close_price, qty)
        p.realized_pnl += pnl
        p.remaining_qty = 0.0
        p.closed = True
        indicators_str = (
            f"entry_ema_fast={p.entry_ema_fast} entry_ema_slow={p.entry_ema_slow} "
            f"entry_volume={p.entry_volume} entry_volume_ma={p.entry_volume_ma} "
            f"entry_rsi={p.entry_rsi:.1f} entry_macd_hist={p.entry_macd_hist:.6f} "
            f"entry_bb=[{p.entry_bb_lower:.4f}..{p.entry_bb_upper:.4f}] entry_atr={p.entry_atr:.6f}"
        )
        self.log.warning(
            f"SL hit (exchange stop) | reason={reason} price={close_price} "
            f"qty={qty:.6f} pnl={pnl:.4f} total_pnl={p.realized_pnl:.4f} | {indicators_str}"
        )
        self.position = None
        self._clear_state()
        return pnl

    def check(self, current_price: float) -> Optional[str]:
        p = self.position
        if p is None or p.closed:
            return None
        if p.direction == "LONG":
            if p.sl_price > 0 and current_price <= p.sl_price:
                return "SL"
            if not p.tp1_hit and current_price >= p.tp1_price:
                return "TP1"
            if p.tp1_hit and current_price >= p.tp2_price:
                return "TP2"
        else:
            if p.sl_price > 0 and current_price >= p.sl_price:
                return "SL"
            if not p.tp1_hit and current_price <= p.tp1_price:
                return "TP1"
            if p.tp1_hit and current_price <= p.tp2_price:
                return "TP2"
        return None

    def apply_hit(self, hit: str, close_price: float) -> tuple[float, str]:
        try:
            p = self.position
            if p is None:
                return 0.0, None
            indicators_str = (
                f"entry_ema_fast={p.entry_ema_fast} entry_ema_slow={p.entry_ema_slow} "
                f"entry_volume={p.entry_volume} entry_volume_ma={p.entry_volume_ma} "
                f"entry_rsi={p.entry_rsi:.1f} entry_macd_hist={p.entry_macd_hist:.6f} "
                f"entry_bb=[{p.entry_bb_lower:.4f}..{p.entry_bb_upper:.4f}] entry_atr={p.entry_atr:.6f}"
            )
                    
            if hit == "SL":
                qty = p.remaining_qty
                pnl = self._calc_pnl(p.direction, p.entry_price, close_price, qty)
                p.realized_pnl += pnl
                p.remaining_qty = 0.0
                p.closed = True
                # Если SL сработал после переноса (tp1_hit=True) — это закрытие по безубытку, пишем TP1
                exit_reason = "TP1" if p.tp1_hit else "SL"
                self.log.warning(
                    f"{exit_reason} hit (SL level) | price={close_price} qty={qty:.6f} pnl={pnl:.4f} "
                    f"total_pnl={p.realized_pnl:.4f} | {indicators_str}"
                )
                self.position = None
                self._clear_state()
                return pnl, exit_reason

            if hit == "TP1":
                # Логируем вход в обработку TP1
                qty_to_close = None
                if p.is_reverse or p.is_recovery:
                    qty_to_close = p.remaining_qty
                else:
                    tp1_qty = round(p.total_qty * self.cfg.tp1_close_pct / 100, 6)
                    qty_to_close = min(tp1_qty, p.remaining_qty)
                # Лог входа в обработку TP1
                self.log.info(f"[TP1_START] position_id={self._trade_id} current_price={close_price} qty_to_close={qty_to_close} total_qty={p.total_qty}")
                    
                if p.is_recovery or p.is_reverse:
                    # Recovery или reverse: TP1 закрывает 100% позиции сразу
                    qty = p.remaining_qty
                    pnl = self._calc_pnl(p.direction, p.entry_price, close_price, qty)
                    p.realized_pnl += pnl
                    p.remaining_qty = 0.0
                    p.closed = True
                    self.log.info(
                        f"TP1 hit [RECOVERY] | price={close_price} qty={qty:.6f} pnl={pnl:.4f} "
                        f"total_pnl={p.realized_pnl:.4f} | {indicators_str}"
                    )
                    self.position = None
                    self._clear_state()
                    # Записываем результат возврата перед возвратом
                    self.log.info(f"[TP1_RETURN] type=tuple pnl={pnl:.4f} exit_reason=TP1")
                    return pnl, "TP1"

                tp1_qty = round(p.total_qty * self.cfg.tp1_close_pct / 100, 6)
                tp1_qty = min(tp1_qty, p.remaining_qty)
                pnl = self._calc_pnl(p.direction, p.entry_price, close_price, tp1_qty)
                p.realized_pnl += pnl
                p.remaining_qty -= tp1_qty
                p.tp1_hit = True
                old_sl = p.sl_price
                # Если TP1 закрыл ВЕСЬ объём (tp1_close_pct=100) — позиция завершена,
                # иначе SL переносится в breakeven и остаток едет к TP2.
                if p.remaining_qty <= 0.000001:
                    p.remaining_qty = 0.0
                    p.closed = True
                    self.log.info(
                        f"TP1 hit (full close) | price={close_price} qty={tp1_qty:.6f} "
                        f"pnl={pnl:.4f} total_pnl={p.realized_pnl:.4f} | {indicators_str}"
                    )
                    self.position = None
                    self._clear_state()
                    return pnl, "TP1"
                p.sl_price = p.entry_price
                self._save_state()
                self.log.info(
                    f"TP1 hit | price={close_price} closed_qty={tp1_qty:.6f} "
                    f"remaining_qty={p.remaining_qty:.6f} pnl={pnl:.4f} | "
                    f"SL moved to breakeven: {old_sl} → {p.entry_price} | {indicators_str}"
                )
                # Записываем результат возврата перед возвратом
                self.log.info(f"[TP1_RETURN] type=tuple pnl={pnl:.4f} exit_reason=None")
                return pnl, None

            if hit == "TP2":
                qty = p.remaining_qty
                pnl = self._calc_pnl(p.direction, p.entry_price, close_price, qty)
                p.realized_pnl += pnl
                p.remaining_qty = 0.0
                p.closed = True
                self.log.info(
                    f"TP2 hit | price={close_price} qty={qty:.6f} pnl={pnl:.4f} "
                    f"total_pnl={p.realized_pnl:.4f} | {indicators_str}"
                )
                self.position = None
                self._clear_state()
                return pnl, "TP2"

            return 0.0, None

        except Exception as e:
            self.log.error(f"[ERROR] TP1 processing failed", exc_info=True)
            raise

    async def apply_hit_async(self, hit: str, close_price: float, candle_time_ms: int) -> float:
        """Применяет hit и репортит в БД."""
        p = self.position
        is_recovery_tp1_full_close = hit == "TP1" and p and p.is_recovery
        tp1_qty = 0.0
        if hit == "TP1" and p and not p.is_recovery:
            tp1_qty = round(p.total_qty * self.cfg.tp1_close_pct / 100, 6)
            tp1_qty = min(tp1_qty, p.remaining_qty)

        # Сохраняем trade_id и tp1_hit ДО apply_hit (который может вызвать _clear_state)
        trade_id_before = self._trade_id
        remaining_before = p.remaining_qty if p else 0
        total_qty_before = p.total_qty if p else 0.0
        tp1_hit_before = p.tp1_hit if p else False
        entry_price_before = p.entry_price if p else close_price
        reject_before = p.reject_reason if p else None
        accumulated_pnl_before = p.realized_pnl if p else 0.0
        is_reverse_before = bool(p and getattr(p, "is_reverse", False))
        direction_before = p.direction if p else "LONG"
        # Фактическое время входа — из позиции (entry_fill_ms, зафиксирован при
        # открытии). Кэш переживает очистку self.position при закрытии.
        self._entry_fill_ms = getattr(p, "entry_fill_ms", None) if p else None
        # Время входа текущей ноги/цикла в мс (UTC). Для reverse fallback — вход
        # исходной ноги; при наличии entry_fill_ms окно начинается с реального входа.
        cycle_entry_ms = await self._entry_time_ms(trade_id_before, prefer_db=is_reverse_before)
        last_event_pnl, exit_reason_override = self.apply_hit(hit, close_price)
        total_trade_pnl = accumulated_pnl_before + last_event_pnl

        # Сохраняем данные исходной ноги ДО того, как apply_hit очистит позицию
        p_before = p
        is_reverse = is_reverse_before
        # Итог reverse пишется отдельным блоком ниже; метка уточняется по
        # фактической цене выхода (REVERSE_TP / REVERSE_SL). Для остальных —
        # прежняя семантика (SL/TP1/TP2 из apply_hit либо сам hit).
        exit_reason = (
            self._classify_reverse_exit(p_before, close_price, hit)
            if is_reverse else (exit_reason_override or hit)
        )
        orig_dir = getattr(p_before, "reversed_from_direction", "") if p_before else ""
        orig_entry = getattr(p_before, "reversed_from_entry", 0.0) if p_before else 0.0
        orig_qty = getattr(p_before, "reversed_from_qty", 0.0) if p_before else 0.0
        orig_leg_pnl = getattr(p_before, "reversed_from_pnl", 0.0) if p_before else 0.0
        pos_mode = getattr(p_before, "mode", "live") if p_before else "live"

        # Реальные данные о сделке с биржи: цена выхода, комиссия и net-PnL цикла.
        # Для rejected-сделок биржи не касаемся — остаётся расчётная логика.
        # entry_ref_* — вход цикла для fallback-поиска первого филла входа, когда
        # entry_fill_ms не зафиксирован (для reverse это вход исходной ноги).
        if is_reverse:
            entry_ref_side = "BUY" if orig_dir == "LONG" else "SELL"
            entry_ref_price = orig_entry
            entry_ref_qty = orig_qty
        else:
            entry_ref_side = "BUY" if direction_before == "LONG" else "SELL"
            entry_ref_price = entry_price_before
            entry_ref_qty = total_qty_before if total_qty_before > 0 else remaining_before
        exchange = None
        if trade_id_before and not reject_before:
            exchange = await self._exchange_cycle_summary(
                cycle_entry_ms, direction_before,
                entry_fill_ms=self._entry_fill_ms,
                entry_ref_side=entry_ref_side,
                entry_ref_price=entry_ref_price,
                entry_ref_qty=entry_ref_qty,
            )
        if exchange is not None:
            self.log.info(
                f"[FILLS] trade_id={trade_id_before} fills={exchange['fills_count']} "
                f"net_pnl={exchange['pnl']:.4f} exit={exchange['exit_price']} "
                f"commission={exchange['commission']:.6f}"
            )
        else:
            self.log.warning(
                f"[FILLS] Exchange fills unavailable for trade {trade_id_before}; "
                f"falling back to calculated values"
            )

        def _close_values(fallback_exit: float, fallback_pnl: float, fallback_qty: float):
            """(exit_price, qty, pnl, commission) из биржи либо fallback (commission=None)."""
            if exchange is None:
                return fallback_exit, fallback_qty, fallback_pnl, None
            return (exchange["exit_price"] or fallback_exit), fallback_qty, exchange["pnl"], exchange["commission"]

        if is_recovery_tp1_full_close:
            await self._verify_position_closed(p.direction, 10)
            real_pnl = await self._fetch_binance_pnl(cycle_entry_ms, trade_id_before)
            pnl_to_use = real_pnl if real_pnl is not None else total_trade_pnl
            ex_exit, ex_qty, ex_pnl, ex_comm = _close_values(close_price, pnl_to_use, remaining_before)
            await self._report_close(ex_exit, ex_qty, ex_pnl, "TP1", entry_price_before, reject_reason=reject_before, commission=ex_comm)
            if exchange is None:
                await self._sync_pnl_from_exchange(cycle_entry_ms, trade_id_before, candle_time_ms)
            else:
                total_trade_pnl = ex_pnl
        elif hit == "TP1":
            # Полное закрытие по TP1 (tp1_close_pct=100, схема TP=2xSL без разделения):
            # позиция закрывается целиком. Обрабатываем как полное закрытие —
            # реальный PnL с биржи + закрытие сделки в БД + очистка состояния.
            # Иначе check() на следующей свече вернёт "TP2" с остаточной qty=0 →
            # лишнее сообщение TP2 после TP1.
            #
            # ВАЖНО: apply_hit() уже выставил self.position=None, поэтому решение
            # принимаем по сохранённым ДО hit значениям. Ориентироваться на
            # self.position нельзя — тогда fully_closed всегда False, и строка в
            # БД навсегда остаётся is_open=1.
            tp1_closes_all = (
                p_before is not None
                and remaining_before > 0.0
                and (remaining_before - tp1_qty) <= 0.000001
            )
            # Reverse закрывается единым результатом в блоке ниже — не дублируем.
            fully_closed = tp1_closes_all and not is_reverse
            if fully_closed:
                await self._verify_position_closed(p.direction, 10)
                real_pnl = await self._fetch_binance_pnl(cycle_entry_ms, trade_id_before)
                pnl_to_use = real_pnl if real_pnl is not None else total_trade_pnl
                if trade_id_before:
                    qty_to_report = remaining_before if remaining_before > 0.0 else total_qty_before
                    ex_exit, ex_qty, ex_pnl, ex_comm = _close_values(close_price, pnl_to_use, qty_to_report)
                    await self._report_close_with_id(trade_id_before, ex_exit, ex_qty, ex_pnl, exit_reason, entry_price_before, reject_reason=reject_before, commission=ex_comm)
                if exchange is None:
                    await self._sync_pnl_from_exchange(cycle_entry_ms, trade_id_before, candle_time_ms)
                    if real_pnl is not None:
                        total_trade_pnl = real_pnl
                else:
                    total_trade_pnl = ex_pnl
                self.position = None
                self._clear_state()
            else:
                self._save_state()
        elif hit in ("SL", "TP2"):
            await self._verify_position_closed(p.direction if p else "LONG", 10)
            real_pnl = await self._fetch_binance_pnl(cycle_entry_ms, trade_id_before)
            pnl_to_use = real_pnl if real_pnl is not None else total_trade_pnl
            # qty для БД: если remaining уже 0 (позиция полностью закрыта
            # TP1 на 100%), сохраняем исходный объём позиции, а не 0.
            qty_to_report = remaining_before if remaining_before > 0.0 else total_qty_before
            ex_exit, ex_qty, ex_pnl, ex_comm = _close_values(close_price, pnl_to_use, qty_to_report)
            # Reverse закрывается единым результатом REVERSE в блоке ниже,
            # поэтому здесь отдельную запись не пишем (иначе двойной close).
            if trade_id_before and not is_reverse:
                await self._report_close_with_id(trade_id_before, ex_exit, ex_qty, ex_pnl, exit_reason, entry_price_before, reject_reason=reject_before, commission=ex_comm)
            if exchange is None:
                if not is_reverse:
                    await self._sync_pnl_from_exchange(cycle_entry_ms, trade_id_before, candle_time_ms)
                # Update local PnL with real value for return
                if real_pnl is not None:
                    total_trade_pnl = real_pnl
            else:
                total_trade_pnl = ex_pnl

        # Если закрылась reverse-позиция — пишем единый результат REVERSE.
        # Убыток исходной ноги (на уровне SL) сохранён в orig_leg_pnl при развороте;
        # отдельно исходную закрывать не нужно — она уже сведена неттингом.
        # Блок не зависит от self.position: apply_hit обычно уже очистил позицию,
        # а закрытие должно репортиться по сохранённому trade_id исходной ноги.
        if is_reverse:
            total_pnl = total_trade_pnl + orig_leg_pnl
            # Объём в БД — реальный размер исходной ноги, а не reverse-ноги.
            report_qty = orig_qty if orig_qty > 0.0 else total_qty_before
            if trade_id_before is None:
                self.log.warning("[REVERSE] No trade_id to report combined result; skip DB close")
            elif report_qty <= 0.0:
                self.log.warning(
                    f"[REVERSE] trade_id={trade_id_before} saved qty unavailable; "
                    f"skip DB close to avoid a zero-qty record"
                )
            else:
                ex_exit, ex_qty, ex_pnl, ex_comm = _close_values(close_price, total_pnl, report_qty)
                # Метка по фактической цене выхода обратной ноги: выход на
                # плановом P3 (tp1/tp2) → REVERSE_TP, иначе REVERSE_SL.
                reverse_reason = self._classify_reverse_exit(p_before, ex_exit, hit)
                await self._report_close_with_id(trade_id_before, ex_exit, report_qty, ex_pnl, reverse_reason, entry_price_before, reject_reason=reject_before, commission=ex_comm)
                if exchange is None:
                    await self._sync_pnl_from_exchange(cycle_entry_ms, trade_id_before, candle_time_ms)
                total_trade_pnl = ex_pnl

        return total_trade_pnl

    async def _verify_position_closed(self, direction: str, max_wait_sec: int = 10) -> None:
        """Wait until exchange confirms position is fully closed (retry every 1s)."""
        if not self.order_mgr:
            return
        for _ in range(max_wait_sec):
            real_qty = await self.order_mgr._get_real_position_qty(direction)
            if real_qty < 0.000001:
                return
            await asyncio.sleep(1.0)
        self.log.warning(f"[POSITION_CHECK] Position still open after {max_wait_sec}s wait")

    async def _fetch_binance_pnl(self, entry_time_ms: int, trade_id: Optional[int]) -> Optional[float]:
        """Fetch real PnL from Binance after position is closed."""
        if not self.order_mgr or not trade_id or entry_time_ms <= 0:
            return None
        try:
            exit_ms = int(time.time() * 1000)
            return await self.order_mgr.get_realized_pnl(self.cfg.symbol, entry_time_ms, exit_ms)
        except Exception:
            return None

    async def _fetch_cycle_fills(self, entry_ms: int, exit_ms: int) -> Optional[list]:
        """Возвращает userTrades-филлы за UTC-корректное окно [entry_ms, exit_ms].

        Валидирует startTime/endTime (положительные, start <= end, end <= now)
        и на ошибке -4181 (Invalid start time) повторяет запрос один раз без
        startTime. Возвращает None, если данные недоступны.

        Возвращённый набор может быть шире реального окна цикла (endTime=now):
        фактическое окно [вход, флэт] вырезает вызывающий _exchange_cycle_summary,
        поэтому филлы следующего/предыдущего цикла сюда не попадут в итог.
        """
        if not self.order_mgr or not getattr(self.order_mgr, "client", None):
            return None
        now_ms = int(time.time() * 1000)
        if entry_ms is None or entry_ms <= 0 or entry_ms > now_ms:
            self.log.warning(f"[FILLS] Invalid startTime={entry_ms}; using now-24h")
            entry_ms = now_ms - 24 * 60 * 60 * 1000
        if exit_ms is None or exit_ms <= 0 or exit_ms > now_ms:
            exit_ms = now_ms
        if exit_ms < entry_ms:
            entry_ms, exit_ms = exit_ms, entry_ms
        start_ms = int(entry_ms)
        end_ms = int(min(exit_ms + 60000, now_ms))
        try:
            return await asyncio.wait_for(
                self.order_mgr.client.futures_account_trades(
                    symbol=self.cfg.symbol,
                    startTime=start_ms,
                    endTime=end_ms,
                ),
                timeout=30,
            )
        except Exception as e:
            if "-4181" in str(e):
                self.log.warning(
                    f"[FILLS] -4181 Invalid start time (start={start_ms}); "
                    f"retrying once without startTime"
                )
                try:
                    return await asyncio.wait_for(
                        self.order_mgr.client.futures_account_trades(
                            symbol=self.cfg.symbol,
                            endTime=end_ms,
                        ),
                        timeout=30,
                    )
                except Exception as e2:
                    self.log.warning(f"[FILLS] retry without startTime failed: {e2}")
                    return None
            self.log.warning(f"[FILLS] userTrades fetch failed: {e}")
            return None

    async def _exchange_cycle_summary(
        self,
        cycle_entry_ms: int,
        position_direction: str,
        entry_fill_ms: Optional[int] = None,
        entry_ref_side: Optional[str] = None,
        entry_ref_price: float = 0.0,
        entry_ref_qty: float = 0.0,
    ) -> Optional[dict]:
        """Считает реальные параметры закрытия цикла по userTrades.

        exit_price — средневзвешенная цена закрывающих филлов (последняя нога),
        commission — суммарная комиссия филлов цикла (USDT),
        pnl — net (сумма realizedPnl минус комиссия),
        last_fill_time — время последнего филла цикла (фактический флэт).

        Окно: [фактический вход, фактический флэт]. Начало — entry_fill_ms
        (зафиксирован при открытии) либо первый филл, совпадающий по
        стороне/цене/объёму входа; конец — последний закрывающий филл, а не
        now. Так филлы предыдущего цикла не затягиваются в текущий.

        Возвращает None, если филлов/закрывающих филлов нет (тогда fallback).
        """
        if not self.order_mgr or not getattr(self.order_mgr, "client", None):
            return None
        now_ms = int(time.time() * 1000)
        fills = await self._fetch_cycle_fills(cycle_entry_ms, now_ms)
        if not fills:
            return None
        position_side = "BUY" if position_direction == "LONG" else "SELL"
        close_side = "SELL" if position_side == "BUY" else "BUY"

        # Нормализуем филлы один раз (time/qty/price), отбрасывая битые записи.
        parsed: list = []
        for f in fills:
            try:
                parsed.append((
                    f,
                    int(f.get("time", 0) or 0),
                    float(f.get("qty", 0) or 0),
                    float(f.get("price", 0) or 0),
                ))
            except (TypeError, ValueError):
                continue
        if not parsed:
            return None

        # --- Начало окна: фактическое время входа цикла ---
        # 1) зафиксированный entry_fill_ms; 2) fallback — первый филл входа,
        # найденный по стороне/цене/объёму; 3) candle entry_time как последний
        # резерв (может затянуть филлы предыдущего цикла — см. фильтр ниже).
        start_ms = int(entry_fill_ms or 0)
        if start_ms <= 0:
            ref_side = entry_ref_side or position_side
            tol = self._price_tol(entry_ref_price if entry_ref_price > 0 else 0.0)
            for f, t_ms, q, pr in sorted(parsed, key=lambda x: x[1]):
                if (f.get("side") or "") != ref_side:
                    continue
                if entry_ref_price > 0 and abs(pr - entry_ref_price) > tol:
                    continue
                if entry_ref_qty > 0 and q > entry_ref_qty * 1.5 + 1e-9:
                    continue
                start_ms = t_ms
                break
        if start_ms <= 0:
            # ОГРАНИЧЕНИЕ: без данных о входе окно начинается с candle entry_time
            # (~5 мин раньше реального входа), поэтому филлы предыдущего цикла
            # могут попасть в сумму. На практике entry_fill_ms фиксируется при
            # открытии, а fallback выше покрывает рестарты.
            start_ms = int(cycle_entry_ms or 0)

        # Время последнего открывающего филла текущей ноги — отсекает филлы
        # предыдущих ног цикла (важно для reverse, где стороны повторяются).
        last_open_ms = 0
        for f, t_ms, q, pr in parsed:
            if (f.get("side") or "") == position_side:
                last_open_ms = max(last_open_ms, t_ms)

        # Закрывающие филлы текущей ноги после её входа формируют цену выхода
        # и фактическое время флэта.
        flat_ms = 0
        for f, t_ms, q, pr in parsed:
            if (f.get("side") or "") != close_side:
                continue
            if t_ms < start_ms:
                continue
            if last_open_ms and t_ms < last_open_ms:
                continue
            if t_ms > flat_ms:
                flat_ms = t_ms
        if flat_ms <= 0:
            # Закрывающих филлов ещё нет — не выдумываем частичный итог.
            return None

        close_qty = 0.0
        close_notional = 0.0
        commission = 0.0
        realized = 0.0
        for f, t_ms, q, pr in parsed:
            # Исключаем филлы предыдущих циклов (до входа) и всё, что позже
            # фактического флэта (это уже следующий цикл).
            if t_ms < start_ms or t_ms > flat_ms:
                continue
            try:
                realized += float(f.get("realizedPnl", 0) or 0)
                if (f.get("commissionAsset") or "") == "USDT":
                    commission += float(f.get("commission", 0) or 0)
            except (TypeError, ValueError):
                continue
            if (f.get("side") or "") != close_side:
                continue
            if last_open_ms and t_ms < last_open_ms:
                continue
            close_qty += q
            close_notional += q * pr
        exit_price = (close_notional / close_qty) if close_qty > 0 else None
        return {
            "exit_price": exit_price,
            "close_qty": close_qty,
            "commission": commission,
            "pnl": realized - commission,
            "fills_count": len(parsed),
            "last_fill_time": flat_ms,
        }

    async def close_open_trade_from_exchange(
        self,
        exit_reason: str = "exchange_closed",
        direction: Optional[str] = None,
    ) -> bool:
        """Закрывает открытую строку trades по факту закрытия позиции на бирже.

        Используется, когда биржа сфлэттенила позицию помимо логики бота
        (pre-entry guard "stale position flattened" / внешнее полное закрытие),
        а строка trades осталась is_open=1 с пустыми exit-полями. Реальные
        exit_price/qty/commission/net-pnl берутся из userTrades; если филлы
        недоступны — используется последняя известная цена и трекерные значения
        (с предупреждением). Никогда не бросает исключение.
        """
        try:
            trade_id = self._trade_id
            if not trade_id:
                self.log.debug(
                    "[EXTERNAL_CLOSE] No tracked open trade row (trade_id is None) — "
                    "nothing to patch"
                )
                return False
            if not self.reporter:
                return False

            row = None
            try:
                row = await self.reporter.get_trade(trade_id)
            except Exception:
                row = None
            is_open_val = row.get("is_open", True) if row is not None else True
            if isinstance(is_open_val, str):
                is_open_val = is_open_val.strip().lower() not in ("", "0", "false", "no")
            if row is not None and not is_open_val:
                self.log.debug(
                    f"[EXTERNAL_CLOSE] trade #{trade_id} already closed — skip"
                )
                return False

            pos = self.position
            row_dir = (row or {}).get("direction")
            eff_direction = (
                row_dir
                or (pos.direction if pos is not None else None)
                or direction
                or "LONG"
            )

            try:
                row_qty = float((row or {}).get("qty") or 0.0)
            except (TypeError, ValueError):
                row_qty = 0.0
            try:
                row_entry = float((row or {}).get("entry_price") or 0.0)
            except (TypeError, ValueError):
                row_entry = 0.0
            if pos is not None:
                qty = float(getattr(pos, "total_qty", 0.0) or 0.0) or row_qty
                entry_price = float(getattr(pos, "entry_price", 0.0) or 0.0) or row_entry
            else:
                qty = row_qty
                entry_price = row_entry

            entry_ms = await self._entry_time_ms(trade_id)
            exchange = await self._exchange_cycle_summary(entry_ms, eff_direction)

            if exchange is not None and exchange.get("exit_price"):
                exit_price = float(exchange["exit_price"])
                commission = float(exchange.get("commission", 0.0) or 0.0)
                pnl = float(exchange.get("pnl", 0.0) or 0.0)
                exit_time = datetime.datetime.utcnow().isoformat()
                last_fill_ms = exchange.get("last_fill_time")
                if last_fill_ms:
                    try:
                        exit_time = datetime.datetime.utcfromtimestamp(
                            int(last_fill_ms) / 1000.0
                        ).isoformat()
                    except (TypeError, ValueError, OverflowError, OSError):
                        exit_time = datetime.datetime.utcnow().isoformat()
                if qty <= 0:
                    qty = float(exchange.get("close_qty") or 0.0)
            else:
                self.log.warning(
                    f"[EXTERNAL_CLOSE] Exchange fills unavailable for trade #{trade_id}; "
                    f"falling back to last known price and tracked values"
                )
                last_price = 0.0
                if self.order_mgr and getattr(self.order_mgr, "client", None):
                    try:
                        ticker = await self.order_mgr.client.futures_symbol_ticker(
                            symbol=self.cfg.symbol
                        )
                        last_price = float(ticker.get("price", 0) or 0.0)
                    except Exception as e:
                        self.log.debug(f"[EXTERNAL_CLOSE] ticker fetch failed: {e}")
                exit_price = last_price or entry_price
                commission = 0.0
                pnl = self._calc_pnl(eff_direction, entry_price, exit_price, qty)
                exit_time = datetime.datetime.utcnow().isoformat()

            if qty <= 0:
                self.log.warning(
                    f"[EXTERNAL_CLOSE] trade #{trade_id} qty unavailable — "
                    f"skip DB close to avoid a zero-qty record"
                )
                return False

            success = await self.reporter.patch_trade(trade_id, {
                "exit_price":  exit_price,
                "qty":         qty,
                "pnl":         pnl,
                "commission":  commission,
                "exit_reason": exit_reason,
                "exit_time":   exit_time,
                "is_open":     False,
                "status":      "closed",
            })
            if not success:
                self.log.warning(
                    f"[EXTERNAL_CLOSE] Failed to patch trade #{trade_id} "
                    f"(exit_reason={exit_reason})"
                )
                return False
            self.log.info(
                f"[EXTERNAL_CLOSE] Closed trade #{trade_id} | exit_price={exit_price} "
                f"qty={qty} pnl={pnl:.4f} commission={commission:.6f} "
                f"reason={exit_reason} exit_time={exit_time}"
            )
            if self.position is None:
                self._trade_id = None
            return True
        except Exception as e:
            self.log.warning(
                f"[EXTERNAL_CLOSE] close_open_trade_from_exchange error: {e}"
            )
            return False

    async def _sync_pnl_from_exchange(self, entry_time_ms: int, trade_id: Optional[int], exit_time_ms: Optional[int] = None) -> None:
        """
        Синхронизирует реальный PnL с биржи для закрытой сделки.
        Запрашивает userTrades за период сделки и обновляет запись в БД.
        Если exit_time_ms не передан, используется текущее время.
        """
        if not self.order_mgr or not trade_id or entry_time_ms <= 0:
            return
        try:
            if exit_time_ms is None:
                exit_time_ms = int(__import__("time").time() * 1000)
            real_pnl = await self.order_mgr.get_realized_pnl(
                self.cfg.symbol, entry_time_ms, exit_time_ms,
            )
            if real_pnl is not None and abs(real_pnl) > 0.0001:
                await self.reporter.patch_trade(trade_id, {"pnl": round(real_pnl, 4)})
                self.log.info(f"[PNL_SYNC] Updated trade #{trade_id} PnL to {real_pnl:.4f} from Binance")
        except Exception as e:
            self.log.warning(f"[PNL_SYNC] Failed to sync PnL: {e}")

    async def sync_unrealized_pnl(self) -> None:
        """
        Синхронизирует нереализованный PnL с биржи для открытой позиции.
        Вызывается периодически для обновления PnL в дашборде.
        """
        if not self.order_mgr or not self.position or self.position.closed:
            return
        try:
            pos_info = await self.order_mgr.get_position_info()
            if pos_info is None:
                return
            real_pnl = pos_info.get("unrealized_pnl", 0)
            if abs(real_pnl) > 0.0001 and self._trade_id:
                await self.reporter.patch_trade(self._trade_id, {"pnl": round(real_pnl, 4)})
                self.log.debug(f"[PNL_SYNC] Updated unrealized Pnl to {real_pnl:.4f}")
        except Exception as e:
            self.log.warning(f"[PNL_SYNC] Failed to sync unrealized PnL: {e}")

    @staticmethod
    def _calc_pnl(direction: str, entry: float, exit_price: float, qty: float) -> float:
        if direction == "LONG":
            return (exit_price - entry) * qty
        else:
            return (entry - exit_price) * qty

    def has_open_position(self) -> bool:
        return self.position is not None and not self.position.closed
        
    def has_rejected_position(self) -> bool:
        """Check if there's a rejected position that should be tracked."""
        return False  # Placeholder for rejected position tracking
