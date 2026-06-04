from dataclasses import dataclass, field
from typing import Optional
import logging

from strategy import Signal
from config import Config


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
        )
        self.log.info(
            f"Position opened | {signal.direction} | entry={signal.entry_price} "
            f"SL={signal.sl_price} TP1={signal.tp1_price} TP2={signal.tp2_price} qty={qty}"
        )

    def check(self, current_price: float) -> Optional[str]:
        """
        Check current price against SL/TP1/TP2.
        Returns: "SL" | "TP1" | "TP2" | None
        """
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
        """
        Apply SL/TP1/TP2 hit. Returns PnL of this partial/full close.
        """
        p = self.position
        if p is None:
            return 0.0

        if hit == "SL":
            qty = p.remaining_qty
            pnl = self._calc_pnl(p.direction, p.entry_price, close_price, qty)
            p.realized_pnl += pnl
            p.remaining_qty = 0.0
            p.closed = True
            self.log.warning(
                f"SL hit | price={close_price} qty={qty:.6f} pnl={pnl:.4f} "
                f"total_pnl={p.realized_pnl:.4f}"
            )
            self.position = None
            return pnl

        if hit == "TP1":
            tp1_qty = round(p.total_qty * self.cfg.tp1_close_pct / 100, 6)
            tp1_qty = min(tp1_qty, p.remaining_qty)
            pnl = self._calc_pnl(p.direction, p.entry_price, close_price, tp1_qty)
            p.realized_pnl += pnl
            p.remaining_qty -= tp1_qty
            p.tp1_hit = True
            self.log.info(
                f"TP1 hit | price={close_price} closed_qty={tp1_qty:.6f} "
                f"remaining_qty={p.remaining_qty:.6f} pnl={pnl:.4f}"
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
                f"total_pnl={p.realized_pnl:.4f}"
            )
            self.position = None
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
