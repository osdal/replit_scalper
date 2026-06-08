import logging
import math
from typing import Optional, Tuple

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


def _round_step(value: float, step: float) -> float:
    """Округляет value вниз до шага step."""
    precision = max(0, round(-math.log10(step)))
    return round(math.floor(value / step) * step, precision)


class OrderManager:
    def __init__(self, cfg: Config, logger: logging.Logger, client: Optional[AsyncClient] = None):
        self.cfg = cfg
        self.log = logger
        self.client = client
        self._step_size: Optional[float] = None   # кэш stepSize для символа
        self._price_precision: Optional[int] = None  # кэш точности цены

    async def _get_symbol_filters(self) -> None:
        """Загружает stepSize и pricePrecision один раз и кэширует."""
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
        """Округляет qty по stepSize символа."""
        if self.cfg.mode != "live":
            return round(qty, 3)
        await self._get_symbol_filters()
        return _round_step(qty, self._step_size)

    async def _adjust_price(self, price: float) -> float:
        """Округляет цену по pricePrecision символа."""
        if self.cfg.mode != "live":
            return round(price, 4)
        await self._get_symbol_filters()
        return round(price, self._price_precision)

    async def _get_fill_price(self, order: dict, fallback: float) -> float:
        """
        Извлекает реальную цену исполнения.
        Binance возвращает avgPrice='0' для маркет-ордеров сразу после создания —
        в этом случае делаем повторный запрос get_order.
        """
        avg = float(order.get("avgPrice", 0))
        if avg > 0:
            return avg

        # Пробуем взять из fills (если есть)
        fills = order.get("fills", [])
        if fills:
            total_qty = sum(float(f["qty"]) for f in fills)
            if total_qty > 0:
                return sum(float(f["price"]) * float(f["qty"]) for f in fills) / total_qty

        # Запрашиваем исполненный ордер отдельно
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
            sl_price = await self._adjust_price(signal.sl_price)
            await self._place_sl(signal.direction, sl_price, qty)
            return entry_price, qty

        else:
            self.log.info(
                f"[PAPER] Would open {signal.direction} {self.cfg.symbol} "
                f"qty={qty} entry={signal.entry_price} "
                f"SL={signal.sl_price} TP1={signal.tp1_price} TP2={signal.tp2_price} "
                f"balance={balance:.2f} USDT"
            )
            return signal.entry_price, qty

    async def close_partial(self, direction: str, qty: float, price: float, reason: str) -> None:
        if self.cfg.mode == "live":
            qty = await self._adjust_qty(qty)
            await self.client.futures_create_order(
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
            qty = await self._adjust_qty(qty)
            await self.client.futures_create_order(
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
            sl_price = await self._adjust_price(entry_price)
            await self._place_sl(direction, sl_price, qty=None)
            self.log.info(f"[LIVE] SL moved to breakeven | stopPrice={sl_price}")
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