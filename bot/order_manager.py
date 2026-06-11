import asyncio
import logging
import math
from typing import Optional, Tuple

from binance import AsyncClient
from binance.enums import (
    SIDE_BUY, SIDE_SELL,
    ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT,
    FUTURE_ORDER_TYPE_STOP_MARKET,
    TIME_IN_FORCE_GTC,
)

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


def _round_step(value: float, step: float) -> float:
    precision = max(0, round(-math.log10(step)))
    return round(math.floor(value / step) * step, precision)


class OrderManager:
    def __init__(self, cfg: Config, logger: logging.Logger, client: Optional[AsyncClient] = None):
        self.cfg = cfg
        self.log = logger
        self.client = client
        self._step_size: Optional[float] = None
        self._price_precision: Optional[int] = None

    # ------------------------------------------------------------------ #
    #  Symbol filters                                                      #
    # ------------------------------------------------------------------ #

    async def _get_symbol_filters(self) -> None:
        if self._step_size is not None:
            return
        info = await self.client.futures_exchange_info()
        for s in info["symbols"]:
            if s["symbol"] == self.cfg.symbol:
                self._price_precision = s.get("pricePrecision", 2)
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        self._step_size = float(f["stepSize"])
                        return
        raise RuntimeError(f"Symbol {self.cfg.symbol} not found in futures_exchange_info")

    async def _adjust_qty(self, qty: float) -> float:
        if self.cfg.mode != "live":
            return round(qty, 3)
        await self._get_symbol_filters()
        return _round_step(qty, self._step_size)

    async def _adjust_price(self, price: float) -> float:
        if self.cfg.mode != "live":
            return round(price, 4)
        await self._get_symbol_filters()
        return round(price, self._price_precision)

    # ------------------------------------------------------------------ #
    #  Position info                                                       #
    # ------------------------------------------------------------------ #

    async def _get_real_position_qty(self, direction: str) -> float:
        try:
            positions = await self.client.futures_position_information(symbol=self.cfg.symbol)
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                if direction == "LONG" and amt > 0:
                    return amt
                if direction == "SHORT" and amt < 0:
                    return abs(amt)
            return 0.0
        except Exception as e:
            self.log.warning(f"[LIVE] Could not fetch position qty: {e}")
            return -1.0

    async def _get_fill_price(self, order: dict, fallback: float) -> float:
        avg = float(order.get("avgPrice", 0))
        if avg > 0:
            return avg

        fills = order.get("fills", [])
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            if total_qty > 0:
                return sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty

        try:
            filled = await self.client.futures_get_order(
                symbol=self.cfg.symbol,
                orderId=order["orderId"],
            )
            avg = float(filled.get("avgPrice", 0))
            if avg > 0:
                return avg
        except Exception as e:
            self.log.warning(f"[LIVE] Could not fetch fill price: {e}")

        self.log.warning(f"[LIVE] Using signal price as fallback: {fallback}")
        return fallback

    async def get_balance(self) -> float:
        if self.cfg.mode == "live":
            account = await self.client.futures_account_balance()
            for asset in account:
                if asset["asset"] == "USDT":
                    return float(asset["balance"])
            raise RuntimeError("USDT balance not found")
        else:
            return self.cfg.paper_balance

    # ------------------------------------------------------------------ #
    #  Cancel helpers                                                      #
    # ------------------------------------------------------------------ #

    async def cancel_all_tp_sl(self, direction: str) -> None:
        """Отменяет все открытые TP и SL ордера по символу."""
        if self.cfg.mode != "live":
            return
        try:
            open_orders = await self.client.futures_get_open_orders(symbol=self.cfg.symbol)
            stop_side = _opposite_side(direction)
            for order in open_orders:
                otype = order.get("type", "")
                side  = order.get("side", "")
                if side == stop_side and otype in (
                    FUTURE_ORDER_TYPE_STOP_MARKET, "TAKE_PROFIT_MARKET", ORDER_TYPE_LIMIT
                ):
                    await self.client.futures_cancel_order(
                        symbol=self.cfg.symbol,
                        orderId=order["orderId"],
                    )
                    self.log.info(f"[LIVE] Cancelled order | type={otype} id={order['orderId']}")
        except Exception as e:
            self.log.warning(f"[LIVE] cancel_all_tp_sl error: {e}")

    # ------------------------------------------------------------------ #
    #  Place orders                                                        #
    # ------------------------------------------------------------------ #

    async def _place_sl(self, direction: str, sl_price: float) -> None:
        stop_side = _opposite_side(direction)
        sl_price = await self._adjust_price(sl_price)
        await self.client.futures_create_order(
            symbol=self.cfg.symbol,
            side=stop_side,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=sl_price,
            closePosition=True,
        )
        self.log.info(f"[LIVE] Stop-loss placed | stopPrice={sl_price}")

    async def _place_tp_limit(self, direction: str, price: float, qty: float) -> None:
        """Выставляет лимитный TP ордер с reduceOnly."""
        side  = _opposite_side(direction)
        price = await self._adjust_price(price)
        qty   = await self._adjust_qty(qty)
        await self.client.futures_create_order(
            symbol=self.cfg.symbol,
            side=side,
            type=ORDER_TYPE_LIMIT,
            price=price,
            quantity=qty,
            timeInForce=TIME_IN_FORCE_GTC,
            reduceOnly=True,
        )
        self.log.info(f"[LIVE] TP limit placed | side={side} price={price} qty={qty}")

    async def _place_all_orders(
        self,
        direction: str,
        total_qty: float,
        sl_price: float,
        tp1_price: float,
        tp2_price: float,
    ) -> None:
        """
        Выставляет SL + TP1 (частичный) + TP2 (остаток) сразу после открытия.
        TP1 закрывает tp1_close_pct% позиции, TP2 — остаток.
        """
        tp1_qty = await self._adjust_qty(total_qty * self.cfg.tp1_close_pct / 100)
        tp2_qty = await self._adjust_qty(total_qty - tp1_qty)

        await self._place_sl(direction, sl_price)
        await self._place_tp_limit(direction, tp1_price, tp1_qty)
        await self._place_tp_limit(direction, tp2_price, tp2_qty)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def open_position(self, signal: Signal) -> Optional[Tuple[float, float]]:
        balance = await self.get_balance()
        raw_qty = calc_quantity(
            balance=balance,
            risk_pct=self.cfg.risk_pct,
            sl_pct=self.cfg.sl_pct,
            entry_price=signal.entry_price,
            leverage=self.cfg.leverage,
        )
        qty = await self._adjust_qty(raw_qty)

        if self.cfg.mode == "live":
            await self._set_leverage()
            order = await self.client.futures_create_order(
                symbol=self.cfg.symbol,
                side=_direction_to_side(signal.direction),
                type=ORDER_TYPE_MARKET,
                quantity=qty,
            )
            entry_price = await self._get_fill_price(order, signal.entry_price)
            self.log.info(
                f"[LIVE] Market order placed | {signal.direction} {self.cfg.symbol} "
                f"qty={qty} entry≈{entry_price}"
            )
            # Выставляем SL + TP1 + TP2 на бирже сразу
            await self._place_all_orders(
                direction=signal.direction,
                total_qty=qty,
                sl_price=signal.sl_price,
                tp1_price=signal.tp1_price,
                tp2_price=signal.tp2_price,
            )
            return entry_price, qty

        else:
            self.log.info(
                f"[PAPER] Would open {signal.direction} {self.cfg.symbol} "
                f"qty={qty} entry={signal.entry_price} "
                f"SL={signal.sl_price} TP1={signal.tp1_price} TP2={signal.tp2_price} "
                f"balance={balance:.2f} USDT"
            )
            return signal.entry_price, qty

    async def close_partial(self, direction: str, qty: float, price: float, reason: str) -> bool:
        """
        В live режиме TP1 уже выставлен на бирже — этот метод вызывается
        когда бот обнаружил срабатывание по цене свечи.
        Проверяем реальную позицию и логируем факт закрытия.
        Возвращает False если позиция уже закрыта на бирже.
        """
        if self.cfg.mode == "live":
            real_qty = await self._get_real_position_qty(direction)
            if real_qty == 0.0:
                self.log.warning(
                    f"[LIVE] Partial close already executed by exchange | {reason}"
                )
                return False
            # TP1 исполнился на бирже — просто логируем
            self.log.info(f"[LIVE] Partial close confirmed | {reason} price≈{price}")
            return True
        else:
            self.log.info(f"[PAPER] Would close partial | {reason} qty={qty} price={price}")
            return True

    async def close_full(self, direction: str, qty: float, price: float, reason: str) -> bool:
        """
        В live режиме TP2/SL уже выставлены на бирже.
        Проверяем реальную позицию — если закрыта биржей, просто логируем.
        Если ещё открыта (например бот поймал SL раньше стопа) — закрываем маркетом.
        Возвращает False если позиция уже закрыта на бирже.
        """
        if self.cfg.mode == "live":
            real_qty = await self._get_real_position_qty(direction)
            if real_qty == 0.0:
                self.log.warning(
                    f"[LIVE] Full close already executed by exchange | {reason}"
                )
                return False

            # Позиция ещё открыта — закрываем маркетом (SL не успел сработать)
            use_qty = await self._adjust_qty(real_qty if real_qty > 0 else qty)
            await self.client.futures_create_order(
                symbol=self.cfg.symbol,
                side=_opposite_side(direction),
                type=ORDER_TYPE_MARKET,
                quantity=use_qty,
                reduceOnly=True,
            )
            self.log.info(f"[LIVE] Full close | {reason} qty={use_qty} price≈{price}")
            return True
        else:
            self.log.info(f"[PAPER] Would close full | {reason} qty={qty} price={price}")
            return True

    async def move_sl_to_breakeven(self, direction: str, entry_price: float) -> None:
        """
        После срабатывания TP1 — отменяем старый SL и выставляем новый на безубыток.
        TP2 на бирже остаётся нетронутым.
        """
        if self.cfg.mode == "live":
            # Отменяем старый SL
            try:
                open_orders = await self.client.futures_get_open_orders(symbol=self.cfg.symbol)
                stop_side = _opposite_side(direction)
                cancelled = 0
                for order in open_orders:
                    if (order.get("type") == FUTURE_ORDER_TYPE_STOP_MARKET
                            and order.get("side") == stop_side):
                        await self.client.futures_cancel_order(
                            symbol=self.cfg.symbol,
                            orderId=order["orderId"],
                        )
                        cancelled += 1
                if cancelled:
                    # Пауза чтобы биржа успела обработать отмену
                    await asyncio.sleep(0.5)
            except Exception as e:
                self.log.warning(f"[LIVE] Could not cancel old SL: {e}")

            # Выставляем новый SL с retry
            for attempt in range(3):
                try:
                    await self._place_sl(direction, entry_price)
                    self.log.info(f"[LIVE] SL moved to breakeven | stopPrice={entry_price}")
                    return
                except Exception as e:
                    if attempt < 2:
                        self.log.warning(f"[LIVE] SL place attempt {attempt+1} failed: {e} — retrying in 1s")
                        await asyncio.sleep(1.0)
                    else:
                        self.log.error(f"[LIVE] Failed to place SL after 3 attempts: {e}")
        else:
            self.log.info(f"[PAPER] Would move SL to breakeven | price={entry_price}")

    async def _set_leverage(self) -> None:
        await self.client.futures_change_leverage(
            symbol=self.cfg.symbol,
            leverage=self.cfg.leverage,
        )