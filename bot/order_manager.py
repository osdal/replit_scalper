import asyncio
import logging
import math
import time
from typing import Optional, Tuple

from binance import AsyncClient
from binance.exceptions import BinanceAPIException
from binance.enums import (
    SIDE_BUY, SIDE_SELL,
    ORDER_TYPE_MARKET, ORDER_TYPE_LIMIT,
    FUTURE_ORDER_TYPE_STOP_MARKET,
    TIME_IN_FORCE_GTC,
)

from config import Config
from rate_limit import is_ban_error, reset_ban_state, wait_for_ban
from strategy import Signal

# TTL кэшей для дорогих read-only вызовов. Баланс — короткий, но достаточный,
# чтобы не дёргать REST несколько раз в одном цикле; инвалидируется после
# любой торговой операции. Позиции — ещё короче (повторные опросы внутри свечи).
BALANCE_CACHE_TTL_SEC = 10.0
POSITION_CACHE_TTL_SEC = 5.0


def _direction_to_side(direction: str) -> str:
    return SIDE_BUY if direction == "LONG" else SIDE_SELL


def _opposite_side(direction: str) -> str:
    return SIDE_SELL if direction == "LONG" else SIDE_BUY


def calc_quantity(
    balance: float,
    risk_pct: float,
    sl_pct: float,
    entry_price: float,
    leverage: int = 1,
) -> float:
    """Рассчитывает размер позиции по формуле риска. leverage не влияет на qty (только на маржу)."""
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
        self._tick_size: Optional[float] = None
        # Биржевые лимиты объёма: LOT_SIZE.maxQty и MARKET_LOT_SIZE.maxQty.
        self._max_qty: Optional[float] = None
        self._market_max_qty: Optional[float] = None
        # AlgoId живого биржевого backstop-стопа (STOP_MARKET, closePosition=true).
        # None = защиты на бирже нет. Персистится через Position.backstop_algo_id.
        self.backstop_algo_id: Optional[int] = None
        # Read-only кэши дорогих вызовов (balance / position info).
        self._balance_cache = {"value": None, "ts": 0.0, "mode": None}
        self._position_cache = {"value": None, "ts": 0.0, "symbol": None}

    # ------------------------------------------------------------------ #
    #  Read-only cache helpers                                             #
    # ------------------------------------------------------------------ #

    def _invalidate_balance_cache(self) -> None:
        self._balance_cache.update(value=None, ts=0.0, mode=None)

    def _invalidate_position_cache(self) -> None:
        self._position_cache.update(value=None, ts=0.0, symbol=None)

    def _invalidate_caches(self) -> None:
        """Сбрасывает оба кэша после торговой операции (entry/close/reverse)."""
        self._invalidate_balance_cache()
        self._invalidate_position_cache()

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
                        self._max_qty = float(f["maxQty"])
                    if f["filterType"] == "MARKET_LOT_SIZE":
                        self._market_max_qty = float(f["maxQty"])
                    if f["filterType"] == "PRICE_FILTER":
                        self._tick_size = float(f["tickSize"])
                return
        raise RuntimeError(f"Symbol {self.cfg.symbol} not found in futures_exchange_info")

    def _order_max_qty(self) -> Optional[float]:
        """Максимум qty на один ордер: MARKET_LOT_SIZE имеет приоритет над LOT_SIZE."""
        caps = [q for q in (self._market_max_qty, self._max_qty) if q and q > 0]
        return min(caps) if caps else None

    async def _adjust_qty(self, qty: float, mode: Optional[str] = None) -> float:
        if mode is None:
            mode = self.cfg.mode
        if mode != "live":
            return round(qty, 3)
        await self._get_symbol_filters()
        return _round_step(qty, self._step_size)

    async def _adjust_price(self, price: float, mode: Optional[str] = None) -> float:
        if mode is None:
            mode = self.cfg.mode
        if mode != "live":
            return round(price, 8)
        await self._get_symbol_filters()
        if self._tick_size:
            precision = max(0, round(-math.log10(self._tick_size)))
            return round(round(price / self._tick_size) * self._tick_size, precision)
        return round(price, self._price_precision)

    # ------------------------------------------------------------------ #
    #  Position info                                                       #
    # ------------------------------------------------------------------ #

    async def _fetch_positions(self):
        """futures_position_information(cfg.symbol) с коротким TTL-кэшем.

        Никогда не бросает: при ошибке API возвращает последний кэш или None.
        При -1003 дожидается снятия бана (rate_limit) и отдаёт кэш/None, чтобы
        caller пропустил цикл, а не долбил REST во время бана.
        """
        now = time.monotonic()
        cache = self._position_cache
        if (cache["value"] is not None and cache["symbol"] == self.cfg.symbol
                and (now - cache["ts"]) < POSITION_CACHE_TTL_SEC):
            self.log.debug(f"[CACHE] position hit | {self.cfg.symbol}")
            return cache["value"]
        try:
            positions = await self.client.futures_position_information(symbol=self.cfg.symbol)
            reset_ban_state()
            cache.update(value=positions, ts=now, symbol=self.cfg.symbol)
            return positions
        except BinanceAPIException as e:
            if is_ban_error(e):
                await wait_for_ban(e, log=self.log)
            else:
                self.log.warning(f"[LIVE] Could not fetch position info: {e}")
        except Exception as e:
            self.log.warning(f"[LIVE] Could not fetch position info: {e}")
        if cache["value"] is not None and cache["symbol"] == self.cfg.symbol:
            self.log.warning(f"[CACHE] using cached position | {self.cfg.symbol}")
            return cache["value"]
        return None

    async def _get_real_position_qty(self, direction: str) -> float:
        positions = await self._fetch_positions()
        if positions is None:
            return -1.0
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if direction == "LONG" and amt > 0:
                return amt
            if direction == "SHORT" and amt < 0:
                return abs(amt)
        return 0.0

    async def _get_real_position_entry(self, direction: str) -> Optional[float]:
        positions = await self._fetch_positions()
        if positions is None:
            return None
        for p in positions:
            amt = float(p.get("positionAmt", 0))
            if (direction == "LONG" and amt > 0) or (direction == "SHORT" and amt < 0):
                return float(p.get("entryPrice", 0))
        return None

    async def get_position_info(self) -> dict | None:
        """
        Возвращает информацию о текущей позиции на Бинансе.
        Returns: {qty, entry_price, unrealized_pnl, direction} or None
        """
        positions = await self._fetch_positions()
        if positions is None:
            return None
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

    async def get_balance(self, mode: Optional[str] = None) -> float:
        if mode is None:
            mode = self.cfg.mode
        if mode != "live":
            return self.cfg.paper_balance

        now = time.monotonic()
        cache = self._balance_cache
        if (cache["value"] is not None and cache["mode"] == mode
                and (now - cache["ts"]) < BALANCE_CACHE_TTL_SEC):
            self.log.debug(f"[CACHE] balance hit | mode={mode}")
            return cache["value"]

        try:
            account = await self.client.futures_account_balance()
            for asset in account:
                if asset["asset"] == "USDT":
                    balance = float(asset["balance"])
                    reset_ban_state()
                    cache.update(value=balance, ts=now, mode=mode)
                    return balance
            raise RuntimeError("USDT balance not found")
        except BinanceAPIException as e:
            if is_ban_error(e):
                await wait_for_ban(e, log=self.log)
            else:
                self.log.warning(f"[LIVE] balance fetch failed: {e}")
        except Exception as e:
            self.log.warning(f"[LIVE] balance fetch failed: {e}")

        # Никогда не роняем бота: отдаём последний известный баланс, иначе 0.0
        # (open_position в этом случае безопасно пропустит вход по qty <= 0).
        if cache["value"] is not None and cache["mode"] == mode:
            self.log.warning(f"[CACHE] using cached balance ${cache['value']:.2f}")
            return cache["value"]
        self.log.warning("[CACHE] no cached balance available, returning 0.0")
        return 0.0

    # ------------------------------------------------------------------ #
    #  Cancel helpers                                                      #
    # ------------------------------------------------------------------ #

    async def cancel_all_tp_sl(self, direction: str, mode: Optional[str] = None) -> None:
        if mode is None:
            mode = self.cfg.mode
        if mode != "live":
            return

        # Снимаем биржевой backstop первым: closePosition-ордер закрыл бы позицию
        # при срабатывании, если бы остался висеть после выхода/разворота.
        await self._cancel_exchange_backstop(self.backstop_algo_id)

        try:
            await self.client.futures_cancel_all_open_orders(symbol=self.cfg.symbol)
            self.log.info(f"[LIVE] Regular orders cancelled | symbol={self.cfg.symbol}")
        except Exception as e:
            self.log.warning(f"[LIVE] cancel regular orders error: {e}")

        try:
            algo_orders = await self.client.futures_get_open_algo_orders(symbol=self.cfg.symbol)
            for order in algo_orders:
                algo_id = order.get("algoId") or order.get("orderId")
                if algo_id:
                    try:
                        await self.client.futures_cancel_algo_order(
                            symbol=self.cfg.symbol,
                            algoId=algo_id
                        )
                        self.log.info(f"[LIVE] Algo order cancelled | algoId={algo_id}")
                    except Exception as ce:
                        self.log.warning(f"[LIVE] Could not cancel algo order {algo_id}: {ce}")
        except Exception as e:
            self.log.debug(f"[LIVE] cancel algo orders: {e}")

        await asyncio.sleep(1.0)

    # ------------------------------------------------------------------ #
    #  Place orders                                                        #
    # ------------------------------------------------------------------ #

    async def _place_sl(self, direction: str, sl_price: float, qty: float = 0.0) -> None:
        # SL-ордер на биржу НЕ выставляется: при достижении цены SL исходная
        # позиция не закрывается, а открывается обратная (см. open_reverse_position).
        # Уровень SL используется только как виртуальный триггер в трекере.
        self.log.info(f"[SL] Exchange stop-loss skipped (reverse strategy) | level={sl_price:.4f}")
        return

    # ------------------------------------------------------------------ #
    #  Exchange-side backstop stop (safety net, NOT the virtual SL)        #
    # ------------------------------------------------------------------ #
    #
    # Виртуальный SL остаётся виртуальным: при его достижении бот разворачивается
    # (см. open_reverse_position), а не закрывает позицию. Биржевой backstop — это
    # ШИРОКИЙ STOP_MARKET с closePosition=true, который нужен только если бот
    # офлайн/забанен и виртуальный SL некому обработать. Он всегда матчится с
    # размером позиции (closePosition=true), поэтому qty в ордер не передаётся.

    async def _place_exchange_backstop(
        self, direction: str, sl_price: float, qty: float = 0.0
    ) -> Optional[int]:
        """Ставит широкий биржевой STOP_MARKET (closePosition=true) как safety-net.

        trigger = LONG: sl_price * (1 - pct/100), SHORT: sl_price * (1 + pct/100),
        где pct = cfg.exchange_sl_backstop_pct, округлённый по tickSize символа.
        Возвращает algoId или None. Никогда не бросает исключение.
        """
        if not getattr(self.cfg, "exchange_sl_backstop_enabled", True):
            self.log.debug("[BACKSTOP] Exchange backstop disabled — skipping")
            return None
        if sl_price is None or sl_price <= 0:
            self.log.debug(f"[BACKSTOP] Skipped: invalid sl_price={sl_price}")
            return None
        if self.client is None or self.cfg.mode != "live":
            self.log.debug("[BACKSTOP] Not live/paper without exchange — skipping")
            return None
        try:
            # Защита от дубля clientAlgoId: снимаем ранее известный backstop,
            # если он почему-то ещё жив (entry/reverse обычно уже отменили его).
            if self.backstop_algo_id:
                await self._cancel_exchange_backstop(self.backstop_algo_id)
            pct = float(getattr(self.cfg, "exchange_sl_backstop_pct", 2.0) or 0.0)
            if direction == "LONG":
                trigger_raw = sl_price * (1 - pct / 100)
                side = SIDE_SELL
                key = "long"
            else:
                trigger_raw = sl_price * (1 + pct / 100)
                side = SIDE_BUY
                key = "short"
            trigger_price = await self._adjust_price(trigger_raw, mode="live")
            if trigger_price <= 0:
                self.log.error(
                    f"[BACKSTOP] Computed trigger price <= 0 | raw={trigger_raw} — skipping"
                )
                return None

            client_algo_id = f"botsl_{self.cfg.symbol[:10]}_{key}"
            resp = await self.client.futures_create_algo_order(
                algoType="CONDITIONAL",
                symbol=self.cfg.symbol,
                side=side,
                type="STOP_MARKET",
                triggerPrice=trigger_price,
                closePosition="true",
                workingType="MARK_PRICE",
                clientAlgoId=client_algo_id,
            )
            algo_id = None
            if resp:
                raw_id = resp.get("algoId")
                if raw_id is not None:
                    try:
                        algo_id = int(raw_id)
                    except (TypeError, ValueError):
                        algo_id = None
            if algo_id is None:
                self.log.error(f"[BACKSTOP] No algoId in response: {resp}")
                return None
            self.backstop_algo_id = algo_id
            self.log.info(
                f"[BACKSTOP] Exchange stop placed | {direction} side={side} "
                f"trigger={trigger_price} sl={sl_price} pct={pct}% "
                f"qty={qty} clientAlgoId={client_algo_id} algoId={algo_id}"
            )
            return algo_id
        except Exception as e:
            self.log.error(f"[BACKSTOP] Failed to place exchange stop: {e}", exc_info=True)
            return None

    async def _cancel_exchange_backstop(self, algo_id: Optional[int]) -> None:
        """Снимает биржевой backstop best-effort. Никогда не бросает исключение.

        DELETE /fapi/v1/algoOrder?algoId=<id>: подписанные параметры идут в query
        string (force_params=True), а не в body — как в рабочем algoDelete
        (grid-orders.ts). Для POST же (см. _place_exchange_backstop) подпись идёт
        в form body, а НЕ в URL (иначе Binance отдаёт -1022).
        """
        if not algo_id:
            return
        if self.client is None or self.cfg.mode != "live":
            return
        try:
            await self.client._request_futures_api(
                "delete", "algoOrder", True,
                force_params=True, data={"algoId": int(algo_id)},
            )
            self.log.info(f"[BACKSTOP] Exchange stop cancelled | algoId={algo_id}")
        except Exception as e:
            self.log.warning(f"[BACKSTOP] Could not cancel exchange stop algoId={algo_id}: {e}")
        finally:
            if self.backstop_algo_id == algo_id:
                self.backstop_algo_id = None

    async def _place_tp_limit(self, direction: str, price: float, qty: float) -> None:
        side  = _opposite_side(direction)
        price = await self._adjust_price(price, mode="live")
        qty   = await self._adjust_qty(qty, mode="live")
        if qty <= 0:
            self.log.warning(f"[LIVE] TP limit qty={qty} <= 0, skipping")
            return
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
        tp1_qty = await self._adjust_qty(total_qty * self.cfg.tp1_close_pct / 100, mode="live")
        tp2_qty = await self._adjust_qty(total_qty - tp1_qty, mode="live")
        self.log.info(f"[ORDER] Placing all orders | sl_qty={total_qty} tp1_qty={tp1_qty} tp2_qty={tp2_qty}")

        await self._place_sl(direction, sl_price, qty=total_qty)
        try:
            await self._place_tp_limit(direction, tp1_price, tp1_qty)
            if tp2_qty <= 0 or self.cfg.tp1_close_pct >= 100:
                # TP1 закрывает весь объём — TP2 не нужен, не спамим warning'ами.
                self.log.debug(
                    f"[ORDER] TP2 skipped | tp1_close_pct={self.cfg.tp1_close_pct} tp2_qty={tp2_qty}"
                )
            else:
                await self._place_tp_limit(direction, tp2_price, tp2_qty)
        except Exception as e:
            self.log.error(f"[ORDER] Failed to place TP orders: {e}", exc_info=True)
            raise

        # Широкий биржевой safety-net поверх виртуального SL (reverse-логика не
        # затрагивается: _place_sl остаётся no-op). Ставим последним, чтобы при
        # провале TP-ордеров не оставить висячий backstop без позиции в трекере.
        await self._place_exchange_backstop(direction, sl_price, qty=total_qty)

    async def open_reverse_position(
        self,
        original_direction: str,
        original_entry: float,
        original_qty: float,
        sl_price: float,
        mode: Optional[str] = None,
    ) -> Optional[Tuple[float, float, float]]:
        """
        Разворот при срабатывании SL.

        Исходная позиция НЕ закрывается отдельным ордером. В обратную сторону
        отправляется рыночный ордер, оставляющий удерживаемый (хедж) объём

            Qh = Qo * |E - P3| / |S - P3|,   P3 = E_rev * (1 ∓ pct)

        где E = original_entry, S = sl_price, Qo = original_qty,
        pct = reverse_breakeven_pct / 100. P3 считается ТОЛЬКО после исполнения
        обратного ордера, от его фактической средней цены E_rev (avgPrice/fills):
        для обратного SHORT P3 = E_rev*(1-pct), для обратного LONG
        P3 = E_rev*(1+pct). Объём отправки send = Qh + Qo, но не больше биржевого
        maxQty (LOT_SIZE / MARKET_LOT_SIZE). TP обратной позиции ставится ровно
        на P3, где суммарный PnL (убыток исходной + прибыль обратной) равен 0.

        Возвращает (entry_price, held_qty, tp_price), где tp_price = P3.
        """
        if mode is None:
            mode = self.cfg.mode
        reverse_dir = "SHORT" if original_direction == "LONG" else "LONG"

        pct = float(getattr(self.cfg, "reverse_breakeven_pct", 0.5) or 0.5) / 100

        # Планировочный P3 от виртуального SL — нужен только для оценки объёма
        # отправки. Реальный P3 пересчитывается ниже от фактической цены реверса.
        if original_direction == "LONG":
            p3_plan = sl_price * (1 - pct)
        else:
            p3_plan = sl_price * (1 + pct)

        held_qty = await self._adjust_qty(
            abs(original_entry - p3_plan) / abs(sl_price - p3_plan) * original_qty, mode=mode
        )
        if held_qty <= 0:
            self.log.warning(f"[REVERSE] held qty={held_qty} <= 0, skipping reverse")
            return None
        send_qty = await self._adjust_qty(held_qty + original_qty, mode=mode)
        if send_qty <= 0:
            self.log.warning(f"[REVERSE] send qty={send_qty} <= 0, skipping reverse")
            return None

        # Биржевой cap по maxQty: send не может превышать максимум одного ордера.
        # Если cap срезал объём — реальный хедж равен send - Qo, а не расчётному Qh.
        cap_bound = False
        if mode == "live":
            await self._get_symbol_filters()
            max_qty = self._order_max_qty()
            if max_qty is not None and send_qty > max_qty:
                capped_send = await self._adjust_qty(max_qty, mode=mode)
                self.log.warning(
                    f"[REVERSE] send qty capped to exchange maxQty | "
                    f"requested={send_qty} capped={capped_send} maxQty={max_qty} "
                    f"planned_held={held_qty} orig_qty={original_qty}"
                )
                send_qty = capped_send
                held_qty = await self._adjust_qty(send_qty - original_qty, mode=mode)
                cap_bound = True
                if send_qty <= 0 or held_qty <= 0:
                    self.log.error(
                        f"[REVERSE] maxQty cap leaves no hedge | send={send_qty} "
                        f"held={held_qty} maxQty={max_qty} — skipping reverse"
                    )
                    return None

        mult = (held_qty / original_qty) if original_qty else 0.0
        self.log.info(
            f"[REVERSE] sizing | dir={original_direction} E={original_entry} S={sl_price} "
            f"P3_plan={p3_plan} pct={pct * 100}% qty={original_qty} held={held_qty} "
            f"send={send_qty} mult={mult}"
        )

        if mode == "live":
            side = _direction_to_side(reverse_dir)
            try:
                await self._set_leverage()
                order = await self.client.futures_create_order(
                    symbol=self.cfg.symbol,
                    side=side,
                    type=ORDER_TYPE_MARKET,
                    quantity=send_qty,
                )
            except Exception as e:
                self.log.error(
                    f"[REVERSE] Failed to place reverse market order "
                    f"(side={side} qty={send_qty}): {e}",
                    exc_info=True,
                )
                return None
            # Разворот изменил позицию/баланс — кэши невалидны.
            self._invalidate_caches()
            entry_price = await self._get_fill_price(order, sl_price)
            self.log.info(
                f"[REVERSE] Sent {side} qty={send_qty} → held {reverse_dir} "
                f"qty={held_qty} @ {entry_price} (original stays open)"
            )
        else:
            entry_price = sl_price
            self.log.info(
                f"[PAPER] Reverse send {reverse_dir} qty={send_qty} → held={held_qty} "
                f"@ {entry_price} (original stays open)"
            )

        if entry_price is None or entry_price <= 0:
            self.log.error(
                f"[REVERSE] invalid reverse fill price={entry_price} — aborting TP"
            )
            return None

        # P3 считается от ФАКТИЧЕСКОЙ средней цены входа реверса E_rev, а не от
        # виртуального SL: иначе TP оказывается по неверную сторону реального
        # филла и исполняется как маркет, ничего не откупая.
        if reverse_dir == "SHORT":
            p3 = entry_price * (1 - pct)
        else:
            p3 = entry_price * (1 + pct)
        tp_price = await self._adjust_price(p3, mode=mode)

        # Геометрия: для SHORT TP обязан быть ниже E_rev, для LONG — выше.
        # Округление по tickSize может схлопнуть его на/за E_rev — тогда сдвигаем
        # минимум на один тик в прибыльную сторону. Если и это невозможно, TP не
        # отправляем (маркет-исполнение по неверной цене недопустимо).
        def _correct_side(tp: float) -> bool:
            return tp < entry_price if reverse_dir == "SHORT" else tp > entry_price

        if not _correct_side(tp_price):
            tick = self._tick_size if mode == "live" else None
            if tick and tick > 0:
                nudged = tp_price - tick if reverse_dir == "SHORT" else tp_price + tick
                tp_price = await self._adjust_price(nudged, mode=mode)
                self.log.warning(
                    f"[REVERSE] P3 rounded to wrong side → nudged | dir={reverse_dir} "
                    f"E_rev={entry_price} raw_p3={p3} nudged_p3={tp_price} tick={tick}"
                )
        tp_ok = _correct_side(tp_price)
        if not tp_ok:
            self.log.error(
                f"[REVERSE] P3 on wrong side of actual fill — aborting TP | "
                f"dir={reverse_dir} E_rev={entry_price} p3={p3} p3_adj={tp_price} "
                f"pct={pct * 100}%"
            )

        # Пересчёт хедж-объёма по РЕАЛЬНОМУ P3: Qh = Qo*|E-P3|/|S-P3|. Отправка
        # шла от планового P3; если реальные значения расходятся материально —
        # логируем оба. Clamp по фактически оставшейся позиции, чтобы TP не
        # пытался закрыть больше, чем есть на бирже.
        actual_held = await self._adjust_qty(send_qty - original_qty, mode=mode)
        if not cap_bound and abs(sl_price - tp_price) > 0:
            held_real = await self._adjust_qty(
                abs(original_entry - tp_price) / abs(sl_price - tp_price) * original_qty,
                mode=mode,
            )
            tol = self._step_size if (mode == "live" and self._step_size) else 0.0
            if held_real > 0:
                if abs(held_real - held_qty) > tol:
                    self.log.info(
                        f"[REVERSE] held recomputed from realized fill | "
                        f"planned={held_qty} realized={held_real} actual={actual_held} "
                        f"E={original_entry} E_rev={entry_price} S={sl_price} P3={tp_price}"
                    )
                held_qty = min(held_real, actual_held) if actual_held > 0 else held_real
            else:
                self.log.warning(
                    f"[REVERSE] realized held qty={held_real} <= 0 — keeping actual "
                    f"held={actual_held} | E={original_entry} S={sl_price} P3={tp_price}"
                )
                held_qty = actual_held
        else:
            held_qty = actual_held

        # TP обратной позиции ровно на P3 (или не ставим, если геометрия невозможна).
        if mode == "live" and held_qty > 0 and tp_ok:
            try:
                await self._place_tp_limit(reverse_dir, tp_price, held_qty)
                self.log.info(f"[REVERSE] TP placed | {reverse_dir} tp={tp_price} qty={held_qty}")
            except Exception as e:
                self.log.error(f"[REVERSE] Failed to place TP: {e}", exc_info=True)

        return entry_price, held_qty, tp_price

    # ------------------------------------------------------------------ #
    #  Public API                                                          #
    # ------------------------------------------------------------------ #

    async def open_position(
        self, signal: Signal,
        recovery_target: Optional[float] = None,
        mode: Optional[str] = None,
    ) -> Optional[Tuple[float, float]]:
        if mode is None:
            mode = self.cfg.mode
        balance = await self.get_balance(mode)
        is_recovery = recovery_target is not None

        # Effective SL distance actually used for the position: the ATR-based SL
        # from the signal when use_fixed_tp_sl=false, otherwise the fixed sl_pct.
        # Sizing uses this so qty * |entry - sl| matches the configured risk budget.
        if (signal.entry_price > 0 and signal.sl_price > 0
                and not getattr(self.cfg, "use_fixed_tp_sl", False)):
            effective_sl_price = signal.sl_price
        elif signal.entry_price > 0:
            if signal.direction == "LONG":
                effective_sl_price = signal.entry_price * (1 - self.cfg.sl_pct / 100)
            else:
                effective_sl_price = signal.entry_price * (1 + self.cfg.sl_pct / 100)
        else:
            effective_sl_price = signal.sl_price
        sl_distance = abs(signal.entry_price - effective_sl_price)
        sl_distance_pct = (sl_distance / signal.entry_price * 100) if signal.entry_price > 0 else 0.0

        if is_recovery:
            # Recovery FIRST — must size to cover debt, ignore fixed sizing
            # Recovery: qty = target_profit / (entry * tp1_pct%)
            # This ensures TP1 hit covers debt + bonus
            raw_qty = recovery_target / (signal.entry_price * self.cfg.tp1_pct / 100)
            # Check margin with leverage
            margin = raw_qty * signal.entry_price / self.cfg.leverage
            if margin > balance:
                self.log.warning(
                    f"[RECOVERY] Insufficient margin | "
                    f"required=${margin:.2f} balance=${balance:.2f} "
                    f"target={recovery_target:.4f} qty={raw_qty:.6f}"
                )
                return None
        elif self.cfg.margin_pct > 0:
            # margin_pct = % от депозита на маржу.
            # margin = round(balance * pct / 100, 1)  (до 1 знака после запятой).
            # Позиция = margin * leverage.
            margin = round(balance * self.cfg.margin_pct / 100, 1)
            raw_qty = (margin * self.cfg.leverage) / signal.entry_price
            self.log.info(
                f"[LIVE] Size by margin_pct | balance=${balance:.2f} "
                f"margin_pct={self.cfg.margin_pct}% margin=${margin:.2f} "
                f"notional=~${margin * self.cfg.leverage:.2f}"
            )
        elif self.cfg.fixed_notional_usd > 0:
            # fixed_notional_usd = МАРЖА (обеспечение).
            # Позиция = margin * leverage, чтобы удовлетворять minNotional.
            raw_qty = (self.cfg.fixed_notional_usd * self.cfg.leverage) / signal.entry_price
        elif self.cfg.fixed_qty > 0:
            raw_qty = self.cfg.fixed_qty
        elif self.cfg.fixed_risk_usd > 0:
            # Fixed loss in USD at SL: qty = risk_usd / |entry - effective_sl|
            if sl_distance > 0:
                raw_qty = self.cfg.fixed_risk_usd / sl_distance
            else:
                raw_qty = self.cfg.fixed_risk_usd / (signal.entry_price * self.cfg.sl_pct / 100)
        else:
            if sl_distance > 0 and balance > 0:
                # risk_pct of balance over the actual SL distance
                raw_qty = (balance * self.cfg.risk_pct / 100) / sl_distance
            else:
                if balance <= 0:
                    self.log.warning(
                        f"[RISK] Balance unavailable (${balance}) — falling back to "
                        f"sl_pct={self.cfg.sl_pct}% for sizing"
                    )
                raw_qty = calc_quantity(
                    balance=balance,
                    risk_pct=self.cfg.risk_pct,
                    sl_pct=self.cfg.sl_pct,
                    entry_price=signal.entry_price,
                    leverage=self.cfg.leverage,
                )
        qty = await self._adjust_qty(raw_qty, mode=mode)

        if qty > 0 and sl_distance > 0:
            self.log.info(
                f"[RISK] entry={signal.entry_price:.6f} sl={effective_sl_price:.6f} "
                f"sl_dist={sl_distance_pct:.3f}% qty={qty} "
                f"risk_usd=${qty * sl_distance:.4f}"
            )

        if qty <= 0:
            self.log.error(
                f"[LIVE] Calculated qty={raw_qty:.6f} rounds to 0 after stepSize adjustment "
                f"(stepSize={self._step_size}) — skipping order. "
                f"Increase risk_pct or reduce leverage."
            )
            return None

        tp1_close_pct = 100 if is_recovery else self.cfg.tp1_close_pct

        if mode == "live":
            await self._set_leverage()
            order = await self.client.futures_create_order(
                symbol=self.cfg.symbol,
                side=_direction_to_side(signal.direction),
                type=ORDER_TYPE_MARKET,
                quantity=qty,
            )
            # Вход изменил позицию/баланс — кэши невалидны.
            self._invalidate_caches()
            entry_price = await self._get_fill_price(order, signal.entry_price)
            self.log.info(
                f"[LIVE] Market order placed | {signal.direction} {self.cfg.symbol} "
                f"qty={qty} entry≈{entry_price}"
                f"{' [RECOVERY]' if is_recovery else ''}"
            )
            # Verify position exists on exchange before continuing
            # Retry up to 3 times with 1s delay — position may appear with slight delay
            real_qty = 0.0
            for attempt in range(3):
                await asyncio.sleep(1.0)
                # Свежий опрос на каждой попытке — кэш не должен маскировать
                # задержку появления позиции на бирже.
                self._invalidate_position_cache()
                real_qty = await self._get_real_position_qty(signal.direction)
                if real_qty > 0:
                    break
            if real_qty < 0.000001:
                self.log.error(
                    f"[LIVE] Position verification failed | "
                    f"order sent but no position found on exchange. "
                    f"qty={qty} direction={signal.direction} order_status={order.get('status')}"
                )
                return None
            if real_qty < qty * 0.9:
                self.log.warning(
                    f"[LIVE] Position partially filled | "
                    f"requested={qty:.6f} actual={real_qty:.6f} ({real_qty/qty*100:.1f}%)"
                )
                # Use actual qty from exchange
                qty = real_qty
                entry_price = await self._get_real_position_entry(signal.direction) or entry_price
            if is_recovery:
                # Recalculate TP to cover target profit
                # target_profit = qty * price_move → price_move = target_profit / qty
                target = recovery_target
                price_move = target / qty
                if signal.direction == "LONG":
                    tp1_price = entry_price + price_move
                else:
                    tp1_price = entry_price - price_move
                adjusted_tp1 = await self._adjust_price(tp1_price, mode="live")
                adjusted_sl = await self._adjust_price(signal.sl_price, mode="live")
                self.log.info(
                    f"[RECOVERY] Orders | target_profit={target:.4f} "
                    f"tp1_price={tp1_price:.4f} sl_price={signal.sl_price:.4f} "
                    f"qty={qty:.6f} entry={entry_price:.4f}"
                )
                await self._place_sl(signal.direction, adjusted_sl, qty=qty)
                await self._place_tp_limit(signal.direction, adjusted_tp1, qty)
                await self._place_exchange_backstop(signal.direction, adjusted_sl, qty=qty)
                return entry_price, qty, tp1_price
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
            if is_recovery:
                target = recovery_target
                price_move = target / qty
                if signal.direction == "LONG":
                    tp1_price = signal.entry_price + price_move
                else:
                    tp1_price = signal.entry_price - price_move
                return signal.entry_price, qty, tp1_price
            return signal.entry_price, qty

    async def close_partial(self, direction: str, qty: float, price: float, reason: str, mode: Optional[str] = None) -> bool:
        if mode is None:
            mode = self.cfg.mode
        if mode == "live":
            real_qty = await self._get_real_position_qty(direction)
            if real_qty == 0.0:
                self.log.warning(f"[LIVE] Partial close already executed by exchange | {reason}")
                return False
            self.log.info(f"[LIVE] Partial close confirmed | {reason} price≈{price}")
            return True
        else:
            self.log.info(f"[PAPER] Would close partial | {reason} qty={qty} price={price}")
            return True

    async def close_full(self, direction: str, qty: float, price: float, reason: str, mode: Optional[str] = None) -> bool:
        if mode is None:
            mode = self.cfg.mode
        if mode == "live":
            real_qty = await self._get_real_position_qty(direction)
            if real_qty == 0.0:
                self.log.warning(f"[LIVE] Close full already executed by exchange | {reason}")
                return False
            self.log.info(f"[LIVE] Full close confirmed | {reason} price≈{price}")
            return True
        else:
            self.log.info(f"[PAPER] Would close full | {reason} qty={qty} price={price}")
            return True

    async def close_dust(self, direction: str, mode: Optional[str] = None) -> bool:
        """Закрывает пылевую позицию (notional < $1) маркет-ордером."""
        if mode is None:
            mode = self.cfg.mode
        if mode != "live":
            return False
        try:
            real_qty = await self._get_real_position_qty(direction)
            if real_qty <= 0:
                return False
            ticker = await self.client.futures_symbol_ticker(symbol=self.cfg.symbol)
            price = float(ticker.get("price", 0))
            notional = real_qty * price
            if notional > 1.0:
                return False
            side = SIDE_SELL if direction == "LONG" else SIDE_BUY
            # Use minimum stepSize to ensure order is accepted by Binance
            step_size = self._step_size if self._step_size else 0.001
            min_qty = max(real_qty, step_size)
            await self.client.futures_create_order(
                symbol=self.cfg.symbol,
                side=side,
                type=ORDER_TYPE_MARKET,
                quantity=min_qty,
                reduceOnly=True,
            )
            # Закрытие изменило позицию/баланс — кэши невалидны.
            self._invalidate_caches()
            self.log.info(f"[LIVE] Dust closed | {direction} qty={min_qty} actual_qty={real_qty} notional=${notional:.4f}")
            return True
        except Exception as e:
            self.log.warning(f"[LIVE] Could not close dust: {e}")
            return False

    async def close_position_market(self, direction: str, mode: Optional[str] = None) -> bool:
        """Закрывает позицию рыночным ордером (reduceOnly)."""
        if mode is None:
            mode = self.cfg.mode
        if mode != "live" or not self.client:
            return False
        try:
            real_qty = await self._get_real_position_qty(direction)
            if real_qty <= 0.000001:
                return False
            await self.cancel_all_tp_sl(direction)
            await asyncio.sleep(1.0)
            side = SIDE_SELL if direction == "LONG" else SIDE_BUY
            step_size = self._step_size if self._step_size else 0.001
            qty = _round_step(real_qty, step_size)
            await self.client.futures_create_order(
                symbol=self.cfg.symbol,
                side=side,
                type=ORDER_TYPE_MARKET,
                quantity=qty,
                reduceOnly=True,
            )
            # Закрытие изменило позицию/баланс — кэши невалидны.
            self._invalidate_caches()
            self.log.info(f"[LIVE] Force closed | {direction} qty={qty}")
            return True
        except Exception as e:
            self.log.warning(f"[LIVE] Force close failed: {e}")
            return False

    async def move_sl_to_breakeven(
        self, direction: str, entry_price: float,
        remaining_qty: float = 0.0, tp2_price: float = 0.0, mode: Optional[str] = None
    ) -> None:
        if mode is None:
            mode = self.cfg.mode
        if mode == "live":
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
        Gets real PnL from Binance for a trade period.
        Uses Income API first (most reliable, includes fees).
        Falls back to userTrades calculation for older trades
        (income API only retains ~7 days).

        Returns None if neither source provides data.
        """
        # Net PnL = realized PnL minus open/close commissions minus funding.
        # This matches Binance position history (net realized PnL), unlike the
        # gross REALIZED_PNL income line which ignores fees.
        try:
            g = {}
            for itype in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
                income = await asyncio.wait_for(
                    self.client.futures_income_history(
                        symbol=symbol,
                        incomeType=itype,
                        startTime=entry_time_ms,
                        endTime=exit_time_ms + 60000,
                        limit=50,
                    ),
                    timeout=30,
                )
                if income:
                    g[itype] = sum(float(i.get("income", "0") or 0) for i in income)
                else:
                    g[itype] = 0.0
            # If we have at least the realized PnL, compute net.
            if "REALIZED_PNL" in g and abs(g["REALIZED_PNL"]) > 0.0001:
                # Binance income values: REALIZED_PNL is gross; COMMISSION and
                # FUNDING_FEE are typically NEGATIVE (amounts deducted).
                # So subtract them from gross by ADDING them:
                #   net = gross + commission + funding  (both <= 0)
                total_pnl = g["REALIZED_PNL"] + g["COMMISSION"] + g["FUNDING_FEE"]
                return total_pnl if abs(total_pnl) > 0.0001 else 0.0
        except asyncio.TimeoutError:
            self.log.warning(f"[LIVE] Income API timeout for {symbol}")
        except Exception as e:
            self.log.warning(f"[LIVE] Income API error: {e}")

        # Fallback: parse userTrades for older trades (no income history)
        try:
            trades = await asyncio.wait_for(
                self.client.futures_account_trades(
                    symbol=symbol,
                    startTime=entry_time_ms,
                    endTime=exit_time_ms + 60000,
                ),
                timeout=30,
            )
            if not trades:
                return None
            total_pnl = 0.0
            entry_commission = 0.0
            position_side = None
            for t in trades:
                realized = float(t.get("realizedPnl", "0") or 0)
                commission = float(t.get("commission", "0") or 0)
                commission_asset = t.get("commissionAsset", "")
                commission_usd = commission if commission_asset == "USDT" else 0.0
                side = t.get("side", "")
                if position_side is None:
                    position_side = "LONG" if side == "BUY" else "SHORT"
                    entry_commission += commission_usd
                elif (position_side == "LONG" and side == "SELL") or \
                     (position_side == "SHORT" and side == "BUY"):
                    total_pnl += realized - commission_usd
                    position_side = None
                else:
                    entry_commission += commission_usd
            total_pnl -= entry_commission
            return total_pnl if abs(total_pnl) > 0.0001 else None
        except asyncio.TimeoutError:
            self.log.warning(f"[LIVE] userTrades timeout for {symbol}")
            return None
        except Exception as e:
            self.log.warning(f"[LIVE] userTrades fallback error: {e}")
            return None
