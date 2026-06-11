import json
import os
from dataclasses import dataclass, asdict
from typing import Optional
import logging

from strategy import Signal
from config import Config

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

    def unrealized_pnl(self, current_price: float) -> float:
        if self.direction == "LONG":
            return (current_price - self.entry_price) * self.remaining_qty
        else:
            return (self.entry_price - current_price) * self.remaining_qty


class PositionTracker:
    def __init__(self, cfg: Config, logger: logging.Logger):
        self.cfg = cfg
        self.log = logger
        self.position: Optional[Position] = None
        self._state_file = _state_file(cfg.symbol)

    # ------------------------------------------------------------------ #
    #  Persistence                                                         #
    # ------------------------------------------------------------------ #

    def _save_state(self) -> None:
        """Сохраняет текущую позицию в JSON файл."""
        if self.position is None:
            self._clear_state()
            return
        p = self.position
        data = {
            "direction":    p.direction,
            "entry_price":  p.entry_price,
            "sl_price":     p.sl_price,
            "tp1_price":    p.tp1_price,
            "tp2_price":    p.tp2_price,
            "total_qty":    p.total_qty,
            "remaining_qty": p.remaining_qty,
            "tp1_hit":      p.tp1_hit,
            "realized_pnl": p.realized_pnl,
            "entry_timestamp": str(p.entry_timestamp) if p.entry_timestamp else None,
            "entry_ema_fast":  p.entry_ema_fast,
            "entry_ema_slow":  p.entry_ema_slow,
            "entry_volume":    p.entry_volume,
            "entry_volume_ma": p.entry_volume_ma,
        }
        try:
            with open(self._state_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            self.log.error(f"[STATE] Failed to save state: {e}")

    def _clear_state(self) -> None:
        """Удаляет файл состояния когда позиция закрыта."""
        try:
            if os.path.exists(self._state_file):
                os.remove(self._state_file)
        except Exception as e:
            self.log.error(f"[STATE] Failed to clear state: {e}")

    def load_state(self) -> bool:
        """
        Загружает позицию из файла состояния.
        Возвращает True если позиция успешно восстановлена.
        """
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
            )
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
    #  Trading logic                                                       #
    # ------------------------------------------------------------------ #

    def open(self, signal: Signal, qty: float) -> None:
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
        )
        self._save_state()
        self.log.info(
            f"Position opened | {signal.direction} | entry={signal.entry_price} "
            f"SL={signal.sl_price} TP1={signal.tp1_price} TP2={signal.tp2_price} qty={qty} | "
            f"indicators: ema_fast={signal.ema_fast} ema_slow={signal.ema_slow} "
            f"volume={signal.volume} volume_ma={signal.volume_ma}"
        )

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
            tp1_qty = round(p.total_qty * self.cfg.tp1_close_pct / 100, 6)
            tp1_qty = min(tp1_qty, p.remaining_qty)
            pnl = self._calc_pnl(p.direction, p.entry_price, close_price, tp1_qty)
            p.realized_pnl += pnl
            p.remaining_qty -= tp1_qty
            p.tp1_hit = True
            old_sl = p.sl_price
            p.sl_price = p.entry_price
            self._save_state()  # обновляем файл — remaining_qty и tp1_hit изменились
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

    @staticmethod
    def _calc_pnl(direction: str, entry: float, exit_price: float, qty: float) -> float:
        if direction == "LONG":
            return (exit_price - entry) * qty
        else:
            return (entry - exit_price) * qty

    def has_open_position(self) -> bool:
        return self.position is not None and not self.position.closed