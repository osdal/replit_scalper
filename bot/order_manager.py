import logging
from typing import Optional

from binance import AsyncClient
from binance.enums import SIDE_BUY, SIDE_SELL, ORDER_TYPE_MARKET, FUTURE_ORDER_TYPE_STOP_MARKET

from config import Config
from strategy import Signal


def _direction_to_side(direction: str) -> str:
    return SIDE_BUY if direction == "LONG" else SIDE_SELL


def _opposite_side(direction: str) -> str:
    return SIDE_SELL if direction == "LONG" else SIDE_BUY


def calc_quantity(
    balance: float,
    risk_pct: float,
    sl_pct: float,
    entry_price: float,
    leverage: int,
) -> float:
    risk_amount = balance * risk_pct / 100
    sl_distance_pct = sl_pct / 100
    quantity = risk_amount / (entry_price * sl_distance_pct)
    return quantity


class OrderManager:
    def __init__(self, cfg: Config, logger: logging.Logger, client: Optional[AsyncClient] = None):
        self.cfg = cfg
        self.log = logger
        self.client = client

    async def get_balance(self) -> float:
        if self.cfg.mode == "live":
            account = await self.client.futures_account_balance()
            for asset in account:
                if asset["asset"] == "USDT":
                    return float(asset["balance"])
            raise RuntimeError("USDT balance not found")
        else:
            return self.cfg.paper_balance

    async def open_position(self, signal: Signal) -> Optional[float]:
        balance = await self.get_balance()
        qty = calc_quantity(
            balance=balance,
            risk_pct=self.cfg.risk_pct,
            sl_pct=self.cfg.sl_pct,
            entry_price=signal.entry_price,
            leverage=self.cfg.leverage,
        )
        qty = round(qty, 3)

        if self.cfg.mode == "live":
            await self._set_leverage()
            order = await self.client.futures_create_order(
                symbol=self.cfg.symbol,
                side=_direction_to_side(signal.direction),
                type=ORDER_TYPE_MARKET,
                quantity=qty,
            )
            entry_price = float(order.get("avgPrice", signal.entry_price))
            self.log.info(
                f"[LIVE] Market order placed | {signal.direction} {self.cfg.symbol} "
                f"qty={qty} entry≈{entry_price}"
            )
            await self._place_sl(signal.direction, signal.sl_price, qty)
            return entry_price

        else:
            self.log.info(
                f"[PAPER] Would open {signal.direction} {self.cfg.symbol} "
                f"qty={qty} entry={signal.entry_price} "
                f"SL={signal.sl_price} TP1={signal.tp1_price} TP2={signal.tp2_price} "
                f"balance={balance:.2f} USDT"
            )
            return signal.entry_price

    async def close_partial(self, direction: str, qty: float, price: float, reason: str) -> None:
        if self.cfg.mode == "live":
            order = await self.client.futures_create_order(
                symbol=self.cfg.symbol,
                side=_opposite_side(direction),
                type=ORDER_TYPE_MARKET,
                quantity=qty,
                reduceOnly=True,
            )
            self.log.info(f"[LIVE] Partial close | {reason} qty={qty} price≈{price}")
        else:
            self.log.info(f"[PAPER] Would close partial | {reason} qty={qty} price={price}")

    async def close_full(self, direction: str, qty: float, price: float, reason: str) -> None:
        if self.cfg.mode == "live":
            order = await self.client.futures_create_order(
                symbol=self.cfg.symbol,
                side=_opposite_side(direction),
                type=ORDER_TYPE_MARKET,
                quantity=qty,
                reduceOnly=True,
            )
            self.log.info(f"[LIVE] Full close | {reason} qty={qty} price≈{price}")
        else:
            self.log.info(f"[PAPER] Would close full | {reason} qty={qty} price={price}")

    async def move_sl_to_breakeven(self, direction: str, entry_price: float) -> None:
        if self.cfg.mode == "live":
            open_orders = await self.client.futures_get_open_orders(symbol=self.cfg.symbol)
            stop_side = _opposite_side(direction)
            for order in open_orders:
                if order.get("type") == FUTURE_ORDER_TYPE_STOP_MARKET and order.get("side") == stop_side:
                    await self.client.futures_cancel_order(
                        symbol=self.cfg.symbol,
                        orderId=order["orderId"],
                    )
            await self._place_sl(direction, entry_price, qty=None)
            self.log.info(f"[LIVE] SL moved to breakeven | stopPrice={entry_price}")
        else:
            self.log.info(f"[PAPER] Would move SL to breakeven | price={entry_price}")

    async def _set_leverage(self) -> None:
        await self.client.futures_change_leverage(
            symbol=self.cfg.symbol,
            leverage=self.cfg.leverage,
        )

    async def _place_sl(self, direction: str, sl_price: float, qty: float) -> None:
        stop_side = _opposite_side(direction)
        await self.client.futures_create_order(
            symbol=self.cfg.symbol,
            side=stop_side,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=sl_price,
            closePosition=True,
        )
        self.log.info(f"[LIVE] Stop-loss placed | stopPrice={sl_price}")
