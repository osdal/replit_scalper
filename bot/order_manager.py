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


def calc_recovery_quantity(
    debt_amount: float,
    bonus_pct: float,
    tp1_pct: float,
    entry_price: float,
    balance: float,
    risk_pct: float,
    sl_pct: float,
    max_pct: Optional[float] = None,
) -> float:
    """
    Рассчитывает размер позиции-компенсатора так, чтобы прибыль
    при полном закрытии на TP1 (100% позиции) покрыла debt_amount + бонус.

    target_profit = debt_amount * (1 + bonus_pct/100)
    tp1_distance   = entry_price * tp1_pct / 100
    qty            = target_profit / tp1_distance

    max_pct (опционально) ограничивает размер позиции в процентах от депозита.
    Например, max_pct=50 означает что позиция не может превышать 50% от balance.
    Если None или 0 — ограничения нет.
    """
    target_profit = debt_amount * (1 + bonus_pct / 100)
    tp1_distance = entry_price * tp1_pct / 100
    if tp1_distance <= 0:
        return 0.0
    raw_qty = target_profit / tp1_distance
    # Если max_pct не задан — возвращаем сырой размер без ограничений
    if max_pct is None or max_pct <= 0:
        return raw_qty
    # Ограничиваем максимальный размер позиции в % от депозита
    max_qty = balance * max_pct / 100
    return min(raw_qty, max_qty)


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
        self._tick_size: Optional[float] = None

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
                    if f["filterType"] == "PRICE_FILTER":
                        self._tick_size = float(f["tickSize"])
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
        if self._tick_size:
            precision = max(0, round(-math.log10(self._tick_size)))
            return round(round(price / self._tick_size) * self._tick_size, precision)
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

    async def get_position_info(self) -> dict | None:
        """
        Возвращает информацию о текущей позиции на Бинансе.
        Returns: {qty, entry_price, unrealized_pnl, direction} or None
        """
        try:
            positions = await self.client.futures_position_information(symbol=self.cfg.symbol)
            for p in positions:
                amt = float(p.get("positionAmt", 0))
                if abs(amt) > 0:
                    return {
                        "qty": abs(amt),
                        "entry_price": float(p.get("entryPrice", 0)),
                        "unrealized_pnl": float(p.get("unrealizedProfit", 0)),
                        "direction": "LONG" if amt > 0 else "SHORT",
                    }
            return None
        except Exception as e:
            self.log.warning(f"[LIVE] Could not fetch position info: {e}")
            return None

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
        if self.cfg.mode != "live":
            return

        try:
            await self.client.futures_cancel_all_open_orders(symbol=self.cfg.symbol)
            self.log.info(f"[LIVE] Regular orders cancelled | symbol={self.cfg.symbol}")
        except Exception as e:
            self.log.warning(f"[LIVE] cancel regular orders error: {e}")

        try:
            algo_orders = await self.client._request_futures_api(
                "get", "openOrders/algo", signed=True,
                data={"symbol": self.cfg.symbol}
            )
            orders = algo_orders.get("orders", []) if isinstance(algo_orders, dict) else []
            for order in orders:
                algo_id = order.get("algoId") or order.get("orderId")
                if algo_id:
                    try:
                        await self.client._request_futures_api(
                            "delete", "order/algo", signed=True,
                            data={"symbol": self.cfg.symbol, "algoId": algo_id}
                        )
                        self.log.info(f"[LIVE] Algo order cancelled | algoId={algo_id}")
                    except Exception as ce:
                        self.log.warning(f"[LIVE] Could not cancel algo order {algo_id}: {ce}")
        except Exception as e:
            self.log.debug(f"[LIVE] cancel algo orders (no algo orders or endpoint N/A): {e}")

        await asyncio.sleep(1.0)

    # ------------------------------------------------------------------ #
    #  Place orders                                                        #
    # ------------------------------------------------------------------ #

    async def _place_sl(self, direction: str, sl_price: float, qty: float = 0.0) -> None:
        stop_side = _opposite_side(direction)
        sl_price = await self._adjust_price(sl_price)

        if qty > 0:
            use_qty = await self._adjust_qty(qty)
        else:
            real_qty = await self._get_real_position_qty(direction)
            use_qty = await self._adjust_qty(real_qty if real_qty > 0 else 0.001)

        await self.client.futures_create_order(
            symbol=self.cfg.symbol,
            side=stop_side,
            type=FUTURE_ORDER_TYPE_STOP_MARKET,
            stopPrice=sl_price,
            quantity=use_qty,
            reduceOnly=True,
        )
        self.log.info(f"[LIVE] Stop-loss placed | stopPrice={sl_price} qty={use_qty}")

    async def _place_tp_limit(self, direction: str, price: float, qty: float) -> None:
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
        tp1_qty = await self._adjust_qty(total_qty * self.cfg.tp1_close_pct / 100)
        tp2_qty = await self._adjust_qty(total_qty - tp1_qty)

        await self._place_sl(direction, sl_price, qty=total_qty)
        await self._place_tp_limit(direction, tp1_price, tp1_qty)
        await self._place_tp_limit(direction, tp2_price, tp2_qty)

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def open_position(
        self, signal: Signal,
        recovery_qty: Optional[float] = None,
    ) -> Optional[Tuple[float, float]]:
        balance = await self.get_balance()
        if recovery_qty is not None:
            raw_qty = recovery_qty
        else:
            raw_qty = calc_quantity(
                balance=balance,
                risk_pct=self.cfg.risk_pct,
                sl_pct=self.cfg.sl_pct,
                entry_price=signal.entry_price,
                leverage=self.cfg.leverage,
            )
        qty = await self._adjust_qty(raw_qty)

        if qty <= 0:
            self.log.error(
                f"[LIVE] Calculated qty={raw_qty:.6f} rounds to 0 after stepSize adjustment "
                f"(stepSize={self._step_size}) — skipping order. "
                f"Increase risk_pct or reduce leverage."
            )
            return None

        tp1_close_pct = 100 if recovery_qty is not None else self.cfg.tp1_close_pct

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
                f"{' [RECOVERY]' if recovery_qty is not None else ''}"
            )
            if recovery_qty is not None:
                await self._place_sl(signal.direction, signal.sl_price, qty=qty)
                await self._place_tp_limit(signal.direction, signal.tp1_price, qty)
            else:
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
        if self.cfg.mode == "live":
            real_qty = await self._get_real_position_qty(direction)
            if real_qty == 0.0:
                self.log.warning(f"[LIVE] Partial close already executed by exchange | {reason}")
                return False
            self.log.info(f"[LIVE] Partial close confirmed | {reason} price≈{price}")
            return True
        else:
            self.log.info(f"[PAPER] Would close partial | {reason} qty={qty} price={price}")
            return True

    async def close_full(self, direction: str, qty: float, price: float, reason: str) -> bool:
        if self.cfg.mode == "live":
            real_qty = await self._get_real_position_qty(direction)
            if real_qty == 0.0:
                self.log.warning(f"[LIVE] Full close already executed by exchange | {reason}")
                return False

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

    async def move_sl_to_breakeven(
        self, direction: str, entry_price: float,
        remaining_qty: float = 0.0, tp2_price: float = 0.0
    ) -> None:
        if self.cfg.mode == "live":
            try:
                await self.client.futures_cancel_all_open_orders(symbol=self.cfg.symbol)
                self.log.info(f"[LIVE] All orders cancelled before SL move")
                await asyncio.sleep(1.0)
            except Exception as e:
                self.log.warning(f"[LIVE] Could not cancel orders: {e}")

            for attempt in range(3):
                try:
                    qty = remaining_qty if remaining_qty > 0 else 0.0
                    await self._place_sl(direction, entry_price, qty=qty)
                    self.log.info(f"[LIVE] SL moved to breakeven | stopPrice={entry_price}")
                    break
                except Exception as e:
                    if attempt < 2:
                        self.log.warning(
                            f"[LIVE] SL place attempt {attempt+1} failed: {e} — retrying in 1.5s"
                        )
                        await asyncio.sleep(1.5)
                    else:
                        self.log.error(f"[LIVE] Failed to place SL after 3 attempts: {e}")
                        return

            if tp2_price > 0 and remaining_qty > 0:
                try:
                    qty = await self._adjust_qty(remaining_qty)
                    await self._place_tp_limit(direction, tp2_price, qty)
                    self.log.info(f"[LIVE] TP2 re-placed after SL move | price={tp2_price} qty={qty}")
                except Exception as e:
                    self.log.error(f"[LIVE] Failed to re-place TP2: {e}")
        else:
            self.log.info(f"[PAPER] Would move SL to breakeven | price={entry_price}")

    async def _set_leverage(self) -> None:
        await self.client.futures_change_leverage(
            symbol=self.cfg.symbol,
            leverage=self.cfg.leverage,
        )

    async def get_realized_pnl(
        self, symbol: str, entry_time_ms: int, exit_time_ms: int,
    ) -> Optional[float]:
        """
        Получает реальный суммарный realized PnL с биржи за период сделки.
        Используется для обновления PnL в БД после закрытия позиции.
        Returns None если не удалось получить данные.
        """
        try:
            trades = await self.client.futures_user_trades(
                symbol=symbol,
                startTime=entry_time_ms,
                endTime=exit_time_ms + 60000,
            )
            if not trades:
                return None
            total_pnl = 0.0
            for t in trades:
                realized = t.get("realizedPnl", "0")
                total_pnl += float(realized)
            return total_pnl if total_pnl != 0.0 else None
        except Exception as e:
            self.log.warning(f"[LIVE] Could not fetch realized PnL from Binance: {e}")
            return None
