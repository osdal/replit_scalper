import json
import os
import datetime
import asyncio
from dataclasses import dataclass
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
            )
            self._trade_id = data.get("trade_id")
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

    async def _report_open(self, signal: Signal, qty: float) -> None:
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
            trade_id = await self.reporter.report_trade(trade_data)
            if trade_id:
                self._trade_id = trade_id
        except Exception as e:
            self.log.debug(f"[REPORTER] report_open error: {e}")

    async def _entry_time_ms(self, trade_id: Optional[int]) -> int:
        """Возвращает время входа позиции в мс — из самого объекта, либо, для
        восстановленных с биржи позиций (entry_timestamp=None), из записи в БД."""
        import datetime
        if self.position and self.position.entry_timestamp:
            try:
                if isinstance(self.position.entry_timestamp, str):
                    return int(datetime.datetime.fromisoformat(self.position.entry_timestamp).timestamp() * 1000)
                return int(self.position.entry_timestamp.timestamp() * 1000)
            except (ValueError, AttributeError):
                pass
        # Fallback: entry_time из БД по trade_id
        if trade_id and self.reporter:
            try:
                rec = await self.reporter.get_trade(trade_id)
                if rec and rec.get("entry_time"):
                    et = str(rec["entry_time"]).replace("Z", "")
                    if et.endswith("Z"):
                        et = et[:-1]
                    if "T" in et:
                        return int(datetime.datetime.fromisoformat(et).timestamp() * 1000)
                    else:
                        return int(datetime.datetime.strptime(et, "%Y-%m-%d %H:%M:%S").timestamp() * 1000)
            except Exception:
                pass
        return 0

    async def _report_close(self, exit_price: float, qty: float, pnl: float, reason: str, entry_price: float = 0.0) -> None:
        if not self.reporter or not self._trade_id:
            return
        try:
            import datetime
            commission, pnl_to_use = self._apply_commission(entry_price, exit_price, qty, pnl)
            # Try to get real PnL from Binance to match position history
            real_pnl = None
            if self.order_mgr:
                entry_time_ms = await self._entry_time_ms(self._trade_id)
                if entry_time_ms > 0:
                    try:
                        exit_time_ms = int(__import__("time").time() * 1000)
                        real_pnl = await self.order_mgr.get_realized_pnl(
                            self.cfg.symbol, entry_time_ms, exit_time_ms,
                        )
                    except Exception:
                        pass
            if real_pnl is not None and abs(real_pnl) > 0.0001:
                pnl_to_use = real_pnl
                # В live-режиме комиссия уже учтена в реальном PnL, показываем её как расчётную.
                commission = self._estimated_commission(entry_price, exit_price, qty)
            success = await self.reporter.patch_trade(self._trade_id, {
                "exit_price":  exit_price,
                "qty":         qty,
                "pnl":         pnl_to_use,
                "commission":  commission,
                "exit_reason": reason,
                "exit_time":   datetime.datetime.utcnow().isoformat(),
                "is_open":     False,
                "status":      "closed",
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
                    "commission":  commission,
                    "exit_reason": reason,
                    "entry_time":  str(p.entry_timestamp).replace(" ", "T") if p and p.entry_timestamp else datetime.datetime.utcnow().isoformat(),
                    "exit_time":   datetime.datetime.utcnow().isoformat(),
                    "is_open":     False,
                    "status":      "closed",
                    "mode":        self.cfg.mode,
                }
                await self.reporter.report_trade(new_trade)
            self._trade_id = None
        except Exception as e:
            self.log.debug(f"[REPORTER] report_close error: {e}")

    async def _report_close_with_id(self, trade_id: int, exit_price: float, qty: float, pnl: float, reason: str, entry_price: float = 0.0) -> None:
        """Закрывает сделку по указанному trade_id (используется после _clear_state)."""
        if not self.reporter:
            return
        try:
            import datetime
            commission, pnl_to_use = self._apply_commission(entry_price, exit_price, qty, pnl)
            # Try to get real PnL from Binance to match position history
            real_pnl = None
            if self.order_mgr:
                entry_time_ms = await self._entry_time_ms(trade_id)
                if entry_time_ms > 0:
                    try:
                        exit_time_ms = int(__import__("time").time() * 1000)
                        real_pnl = await self.order_mgr.get_realized_pnl(
                            self.cfg.symbol, entry_time_ms, exit_time_ms,
                        )
                    except Exception:
                        pass
            if real_pnl is not None and abs(real_pnl) > 0.0001:
                pnl_to_use = real_pnl
                commission = self._estimated_commission(entry_price, exit_price, qty)
            success = await self.reporter.patch_trade(trade_id, {
                "exit_price":  exit_price,
                "qty":         qty,
                "pnl":         pnl_to_use,
                "commission":  commission,
                "exit_reason": reason,
                "exit_time":   datetime.datetime.utcnow().isoformat(),
                "is_open":     False,
                "status":      "closed",
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
                    "commission":  commission,
                    "exit_reason": reason,
                    "entry_time":  str(p.entry_timestamp).replace(" ", "T") if p and p.entry_timestamp else datetime.datetime.utcnow().isoformat(),
                    "exit_time":   datetime.datetime.utcnow().isoformat(),
                    "is_open":     False,
                    "status":      "closed",
                    "mode":        self.cfg.mode,
                }
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
    #  Trading logic                                                       #
    # ------------------------------------------------------------------ #

    def open(
        self, signal: Signal, qty: float,
        is_recovery: bool = False, recovery_chain_id: Optional[int] = None,
    ) -> None:
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
        )
        self._trade_id = None
        self._save_state()
        tag = " [RECOVERY]" if is_recovery else ""
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
    ) -> None:
        """Открывает позицию и репортит в БД."""
        self.open(signal, qty, is_recovery=is_recovery, recovery_chain_id=recovery_chain_id)
        await self._report_open(signal, qty)
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
            if current_price <= p.sl_price:
                return "SL"
            if not p.tp1_hit and current_price >= p.tp1_price:
                return "TP1"
            if p.tp1_hit and current_price >= p.tp2_price:
                return "TP2"
        else:
            if current_price >= p.sl_price:
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
                if p.is_recovery:
                    qty_to_close = p.remaining_qty
                else:
                    tp1_qty = round(p.total_qty * self.cfg.tp1_close_pct / 100, 6)
                    qty_to_close = min(tp1_qty, p.remaining_qty)
                # Лог входа в обработку TP1
                self.log.info(f"[TP1_START] position_id={self._trade_id} current_price={close_price} qty_to_close={qty_to_close} total_qty={p.total_qty}")
                    
                if p.is_recovery:
                    # Recovery-позиция: TP1 закрывает 100% позиции сразу
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
        # entry_timestamp может быть строкой из JSON или datetime объектом
        entry_time_ms = 0
        if p and p.entry_timestamp:
            try:
                if isinstance(p.entry_timestamp, str):
                    entry_time_ms = int(datetime.datetime.fromisoformat(p.entry_timestamp).timestamp() * 1000)
                else:
                    entry_time_ms = int(p.entry_timestamp.timestamp() * 1000)
            except (ValueError, AttributeError):
                entry_time_ms = 0
        accumulated_pnl_before = p.realized_pnl if p else 0.0
        last_event_pnl, exit_reason_override = self.apply_hit(hit, close_price)
        total_trade_pnl = accumulated_pnl_before + last_event_pnl

        if is_recovery_tp1_full_close:
            await self._verify_position_closed(p.direction, 10)
            real_pnl = await self._fetch_binance_pnl(entry_time_ms, trade_id_before)
            pnl_to_use = real_pnl if real_pnl is not None else total_trade_pnl
            await self._report_close(close_price, remaining_before, pnl_to_use, "TP1", entry_price_before)
            await self._sync_pnl_from_exchange(entry_time_ms, trade_id_before, candle_time_ms)
        elif hit == "TP1":
            # Полное закрытие по TP1 (tp1_close_pct=100, схема TP=2xSL без разделения):
            # позиция закрывается целиком. Обрабатываем как полное закрытие —
            # реальный PnL с биржи + закрытие сделки в БД + очистка состояния.
            # Иначе check() на следующей свече вернёт "TP2" с остаточной qty=0 →
            # лишнее сообщение TP2 после TP1.
            fully_closed = self.position is not None and self.position.remaining_qty <= 0.000001
            if fully_closed:
                await self._verify_position_closed(p.direction, 10)
                real_pnl = await self._fetch_binance_pnl(entry_time_ms, trade_id_before)
                pnl_to_use = real_pnl if real_pnl is not None else total_trade_pnl
                if trade_id_before:
                    qty_to_report = remaining_before if remaining_before > 0.0 else total_qty_before
                    await self._report_close_with_id(trade_id_before, close_price, qty_to_report, pnl_to_use, "TP1", entry_price_before)
                await self._sync_pnl_from_exchange(entry_time_ms, trade_id_before, candle_time_ms)
                if real_pnl is not None:
                    total_trade_pnl = real_pnl
                self.position = None
                self._clear_state()
            else:
                self._save_state()
        elif hit in ("SL", "TP2"):
            exit_reason = exit_reason_override or ("TP1" if (hit == "SL" and tp1_hit_before) else hit)
            await self._verify_position_closed(p.direction, 10)
            real_pnl = await self._fetch_binance_pnl(entry_time_ms, trade_id_before)
            pnl_to_use = real_pnl if real_pnl is not None else total_trade_pnl
            if trade_id_before:
                # qty для БД: если remaining уже 0 (позиция полностью закрыта
                # TP1 на 100%), сохраняем исходный объём позиции, а не 0.
                qty_to_report = remaining_before if remaining_before > 0.0 else total_qty_before
                await self._report_close_with_id(trade_id_before, close_price, qty_to_report, pnl_to_use, exit_reason, entry_price_before)
            await self._sync_pnl_from_exchange(entry_time_ms, trade_id_before, candle_time_ms)
            # Update local PnL with real value for return
            if real_pnl is not None:
                total_trade_pnl = real_pnl

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
            exit_ms = int(__import__("time").time() * 1000)
            return await self.order_mgr.get_realized_pnl(self.cfg.symbol, entry_time_ms, exit_ms)
        except Exception:
            return None

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
