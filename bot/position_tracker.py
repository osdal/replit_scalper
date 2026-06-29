import json
import os
from dataclasses import dataclass
from typing import Optional, TYPE_CHECKING
import logging

from strategy import Signal
from config import Config

if TYPE_CHECKING:
    from db_reporter import DbReporter

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
    is_recovery: bool = False       # True если это компенсирующая сделка
    recovery_chain_id: Optional[int] = None

    def unrealized_pnl(self, current_price: float) -> float:
        if self.direction == "LONG":
            return (current_price - self.entry_price) * self.remaining_qty
        else:
            return (self.entry_price - current_price) * self.remaining_qty


class PositionTracker:
    def __init__(self, cfg: Config, logger: logging.Logger, reporter: Optional["DbReporter"] = None):
        self.cfg = cfg
        self.log = logger
        self.reporter = reporter
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
            "is_recovery":     p.is_recovery,
            "recovery_chain_id": p.recovery_chain_id,
            "trade_id":        self._trade_id,
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
                is_recovery=data.get("is_recovery", False),
                recovery_chain_id=data.get("recovery_chain_id"),
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
                "mode":        self.cfg.mode,
                "ema_fast":    signal.ema_fast,
                "ema_slow":    signal.ema_slow,
                "volume":      signal.volume,
                "volume_ma":   signal.volume_ma,
            }
            trade_id = await self.reporter.report_trade(trade_data)
            if trade_id:
                self._trade_id = trade_id
        except Exception as e:
            self.log.debug(f"[REPORTER] report_open error: {e}")

    async def _report_close(self, exit_price: float, qty: float, pnl: float, reason: str) -> None:
        if not self.reporter or not self._trade_id:
            return
        try:
            import datetime
            success = await self.reporter.patch_trade(self._trade_id, {
                "exit_price":  exit_price,
                "qty":         qty,
                "pnl":         pnl,
                "exit_reason": reason,
                "exit_time":   datetime.datetime.utcnow().isoformat(),
                "is_open":     False,
            })
            if not success:
                # Запись не найдена (например после очистки БД) — создаём новую
                self.log.warning(f"[REPORTER] trade #{self._trade_id} not found, creating new record")
                p = self.position
                new_trade = {
                    "symbol":      self.cfg.symbol,
                    "direction":   p.direction if p else "LONG",
                    "entry_price": p.entry_price if p else exit_price,
                    "exit_price":  exit_price,
                    "qty":         qty,
                    "pnl":         pnl,
                    "exit_reason": reason,
                    "entry_time":  str(p.entry_timestamp).replace(" ", "T") if p and p.entry_timestamp else datetime.datetime.utcnow().isoformat(),
                    "exit_time":   datetime.datetime.utcnow().isoformat(),
                    "is_open":     False,
                    "mode":        self.cfg.mode,
                }
                await self.reporter.report_trade(new_trade)
            self._trade_id = None
        except Exception as e:
            self.log.debug(f"[REPORTER] report_close error: {e}")

    async def _report_close_with_id(self, trade_id: int, exit_price: float, qty: float, pnl: float, reason: str) -> None:
        """Закрывает сделку по указанному trade_id (используется после _clear_state)."""
        if not self.reporter:
            return
        try:
            import datetime
            success = await self.reporter.patch_trade(trade_id, {
                "exit_price":  exit_price,
                "qty":         qty,
                "pnl":         pnl,
                "exit_reason": reason,
                "exit_time":   datetime.datetime.utcnow().isoformat(),
                "is_open":     False,
            })
            if not success:
                # Запись не найдена (например после очистки БД) — создаём новую
                self.log.warning(f"[REPORTER] trade #{trade_id} not found, creating new record")
                p = self.position
                new_trade = {
                    "symbol":      self.cfg.symbol,
                    "direction":   p.direction if p else "LONG",
                    "entry_price": p.entry_price if p else exit_price,
                    "exit_price":  exit_price,
                    "qty":         qty,
                    "pnl":         pnl,
                    "exit_reason": reason,
                    "entry_time":  str(p.entry_timestamp).replace(" ", "T") if p and p.entry_timestamp else datetime.datetime.utcnow().isoformat(),
                    "exit_time":   datetime.datetime.utcnow().isoformat(),
                    "is_open":     False,
                    "mode":        self.cfg.mode,
                }
                await self.reporter.report_trade(new_trade)
        except Exception as e:
            self.log.debug(f"[REPORTER] report_close_with_id error: {e}")

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
            is_recovery=is_recovery,
            recovery_chain_id=recovery_chain_id,
        )
        self._trade_id = None
        self._save_state()
        tag = " [RECOVERY]" if is_recovery else ""
        self.log.info(
            f"Position opened{tag} | {signal.direction} | entry={signal.entry_price} "
            f"SL={signal.sl_price} TP1={signal.tp1_price} TP2={signal.tp2_price} qty={qty} | "
            f"indicators: ema_fast={signal.ema_fast} ema_slow={signal.ema_slow} "
            f"volume={signal.volume} volume_ma={signal.volume_ma}"
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
            f"entry_volume={p.entry_volume} entry_volume_ma={p.entry_volume_ma}"
        )
        self.log.warning(
            f"SL hit (exchange stop) | reason={reason} price={close_price} "
            f"qty={qty:.6f} pnl={pnl:.4f} total_pnl={p.realized_pnl:.4f} | {indicators_str}"
        )
        self.position = None
        self._clear_state()
        return pnl

    async def force_close_async(self, reason: str, close_price: float) -> float:
        pnl = self.force_close(reason, close_price)
        await self._report_close(close_price, 0, pnl, "SL")
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

    def apply_hit(self, hit: str, close_price: float) -> float:
        p = self.position
        if p is None:
            return 0.0
        indicators_str = (
            f"entry_ema_fast={p.entry_ema_fast} entry_ema_slow={p.entry_ema_slow} "
            f"entry_volume={p.entry_volume} entry_volume_ma={p.entry_volume_ma}"
        )

        if hit == "SL":
            qty = p.remaining_qty
            pnl = self._calc_pnl(p.direction, p.entry_price, close_price, qty)
            p.realized_pnl += pnl
            p.remaining_qty = 0.0
            p.closed = True
            self.log.warning(
                f"SL hit | price={close_price} qty={qty:.6f} pnl={pnl:.4f} "
                f"total_pnl={p.realized_pnl:.4f} | {indicators_str}"
            )
            self.position = None
            self._clear_state()
            return pnl

        if hit == "TP1":
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
                return pnl

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
            return pnl

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
            return pnl

        return 0.0

    async def apply_hit_async(self, hit: str, close_price: float) -> float:
        """Применяет hit и репортит в БД."""
        p = self.position
        is_recovery_tp1_full_close = hit == "TP1" and p and p.is_recovery
        tp1_qty = 0.0
        if hit == "TP1" and p and not p.is_recovery:
            tp1_qty = round(p.total_qty * self.cfg.tp1_close_pct / 100, 6)
            tp1_qty = min(tp1_qty, p.remaining_qty)

        # Сохраняем trade_id ДО apply_hit (который может вызвать _clear_state)
        trade_id_before = self._trade_id
        remaining_before = p.remaining_qty if p else 0
        # ВАЖНО: apply_hit() возвращает pnl только этого конкретного события
        # (например только убыток от SL на остатке позиции), а не общий
        # результат сделки. Если до этого был частичный TP1 — его прибыль
        # уже накоплена в p.realized_pnl, но не в возвращаемом значении.
        # Сохраняем accumulated_pnl_before, чтобы для финального закрытия
        # репортить в БД суммарный результат всей сделки (TP1 + SL/TP2),
        # а не только последний кусок. Иначе сделка, реально закрытая в
        # плюс (TP1 прибыль > SL убыток на остатке), попадёт в БД и
        # дашборд как убыточная по величине одного только SL.
        accumulated_pnl_before = p.realized_pnl if p else 0.0
        last_event_pnl = self.apply_hit(hit, close_price)
        total_trade_pnl = accumulated_pnl_before + last_event_pnl

        if is_recovery_tp1_full_close:
            # Recovery TP1 — это полное закрытие, репортим как close
            await self._report_close(close_price, remaining_before, total_trade_pnl, "TP1")
        elif hit == "TP1":
            # TP1 — частичное закрытие, не репортим в БД, только сохраняем состояние
            self._save_state()
        elif hit in ("SL", "TP2"):
            # SL или TP2 — полное закрытие, репортим используя сохранённый trade_id
            # и СУММАРНЫЙ pnl сделки (включая прибыль/убыток с предыдущего TP1, если был)
            if trade_id_before:
                await self._report_close_with_id(trade_id_before, close_price, remaining_before, total_trade_pnl, hit)

        # Возвращаем СУММАРНЫЙ pnl всей сделки (TP1 + это событие), а не только
        # последнего события. main.py использует это возвращаемое значение
        # для recovery-логики (recovery.report(pnl=...) — решение о создании
        # цепочки компенсации) и логирования итогового результата — оба
        # места ожидают чистый результат сделки целиком, иначе сделка,
        # реально закрытая в плюс (TP1 прибыль > SL убыток на остатке),
        # была бы неверно помечена как убыточная и запустила бы recovery
        # на пустом месте.
        return total_trade_pnl

    @staticmethod
    def _calc_pnl(direction: str, entry: float, exit_price: float, qty: float) -> float:
        if direction == "LONG":
            return (exit_price - entry) * qty
        else:
            return (entry - exit_price) * qty

    def has_open_position(self) -> bool:
        return self.position is not None and not self.position.closed
