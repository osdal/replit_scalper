import asyncio
import datetime
import json as _json
import logging
import math
import os
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

from config import Config, reverse_w_pct_for_step
from rate_limit import is_ban_error, reset_ban_state, wait_for_ban
from strategy import Signal

# Аудит ордеров (только live): пишем в logs/<env>/orders_audit.jsonl.
BOT_ENV = (os.getenv("BOT_ENV") or "testnet").strip().lower() or "testnet"
_AUDIT_ENABLED = BOT_ENV == "live"
_AUDIT_PATH = os.path.join("logs", BOT_ENV, "orders_audit.jsonl")


def _live_default_margin() -> float:
    """Глобальный дефолт маржи для live (USD, env LIVE_DEFAULT_MARGIN_USD)."""
    if BOT_ENV != "live":
        return 0.0
    try:
        return float(os.getenv("LIVE_DEFAULT_MARGIN_USD", "0") or 0)
    except (TypeError, ValueError):
        return 0.0


def _audit(event: str, **fields) -> None:
    """Пишет аудит-строку по ордеру (no-op вне live). Никогда не бросает."""
    if not _AUDIT_ENABLED:
        return
    try:
        rec = {"ts": datetime.datetime.utcnow().isoformat() + "Z", "env": BOT_ENV, "event": event}
        rec.update(fields)
        os.makedirs(os.path.dirname(_AUDIT_PATH), exist_ok=True)
        with open(_AUDIT_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(rec, ensure_ascii=False, default=str) + "\n")
    except Exception:
        pass

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


def _extract_max_leverage(data: object) -> Optional[int]:
    """Достаёт максимально допустимое плечо из ответа futures_leverage_bracket.

    python-binance возвращает ``[{"symbol": ..., "brackets": [...]}]``, но форма
    может отличаться (dict, список bracket-записей). Максимум — ``initialLeverage``
    самой низкой корзины (bracket 1 / минимальный notionalFloor). Парсим защитно
    и возвращаем None, если распознать не удалось.
    """
    blocks = []
    if isinstance(data, dict):
        blocks.append(data)
    elif isinstance(data, list):
        blocks.extend(item for item in data if isinstance(item, dict))

    for blk in blocks:
        brackets = blk.get("brackets")
        if brackets is None and ("initialLeverage" in blk or "leverage" in blk):
            # Ответ уже является одной bracket-записью.
            brackets = [blk]
        if not isinstance(brackets, list):
            continue
        best_key = None
        best_lev = None
        for b in brackets:
            if not isinstance(b, dict):
                continue
            raw_lev = b.get("initialLeverage", b.get("leverage"))
            try:
                lev = int(float(raw_lev))
            except (TypeError, ValueError):
                continue
            if lev < 1:
                continue
            try:
                bracket_no = int(b.get("bracket", 1))
            except (TypeError, ValueError):
                bracket_no = 1
            try:
                floor = float(b.get("notionalFloor", 0) or 0)
            except (TypeError, ValueError):
                floor = 0.0
            key = (bracket_no, floor)
            if best_key is None or key < best_key:
                best_key = key
                best_lev = lev
        if best_lev is not None:
            return best_lev
    return None


def _extract_notional_cap(data: object, leverage: int) -> Optional[float]:
    """Максимальный нотионал (USD) для выбранного плеча из futures_leverage_bracket.

    Берём самую «узкую» корзину, которая ещё допускает наше плечо
    (initialLeverage >= leverage) — её notionalCap и есть потолок позиции.
    """
    blocks = []
    if isinstance(data, dict):
        blocks.append(data)
    elif isinstance(data, list):
        blocks.extend(item for item in data if isinstance(item, dict))

    for blk in blocks:
        brackets = blk.get("brackets")
        if brackets is None and ("initialLeverage" in blk or "leverage" in blk):
            brackets = [blk]
        if not isinstance(brackets, list):
            continue
        eligible = []
        for b in brackets:
            if not isinstance(b, dict):
                continue
            try:
                lev = int(float(b.get("initialLeverage", b.get("leverage"))))
                cap = float(b.get("notionalCap", 0) or 0)
                floor = float(b.get("notionalFloor", 0) or 0)
            except (TypeError, ValueError):
                continue
            if lev >= leverage and cap > 0:
                eligible.append((floor, cap))
        if eligible:
            # минимальный floor → самый низкий cap для нашего плеча
            eligible.sort(key=lambda x: x[0])
            return eligible[0][1]
    return None


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
        # Биржевой минимум нотионала (MIN_NOTIONAL.notional), USD. 0 = нет/неизвестно.
        self._min_notional: Optional[float] = None
        # AlgoId живого биржевого backstop-стопа (STOP_MARKET, closePosition=true).
        # None = защиты на бирже нет. Персистится через Position.backstop_algo_id.
        self.backstop_algo_id: Optional[int] = None
        # Read-only кэши дорогих вызовов (balance / position info).
        self._balance_cache = {"value": None, "free": None, "ts": 0.0, "mode": None}
        self._position_cache = {"value": None, "ts": 0.0, "symbol": None}
        # Кэш максимально допустимого плеча символа (bracket 1 initialLeverage).
        self._max_leverage_cache: Optional[int] = None
        # Кэш биржевого потолка нотионала для текущего плеча.
        self._notional_cap_cache: Optional[float] = None
        # Кэш equity (с нереализованным PnL) для потолка % от счёта.
        self._equity_cache = {"value": None, "ts": 0.0}
        # Последнее залогированное (configured, effective) для [LEVERAGE] clamped,
        # чтобы логировать зажим на старте и при каждом изменении, но не каждый вход.
        self._last_leverage_log: Optional[Tuple[int, int]] = None

    # ------------------------------------------------------------------ #
    #  Read-only cache helpers                                             #
    # ------------------------------------------------------------------ #

    def _invalidate_balance_cache(self) -> None:
        self._balance_cache.update(value=None, free=None, ts=0.0, mode=None)

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
                self._min_notional = 0.0
                for f in s["filters"]:
                    if f["filterType"] == "LOT_SIZE":
                        self._step_size = float(f["stepSize"])
                        self._max_qty = float(f["maxQty"])
                    if f["filterType"] == "MARKET_LOT_SIZE":
                        self._market_max_qty = float(f["maxQty"])
                    if f["filterType"] == "PRICE_FILTER":
                        self._tick_size = float(f["tickSize"])
                    if f["filterType"] == "MIN_NOTIONAL":
                        try:
                            self._min_notional = float(f.get("notional", 0) or 0)
                        except (TypeError, ValueError):
                            self._min_notional = 0.0
                return
        raise RuntimeError(f"Symbol {self.cfg.symbol} not found in futures_exchange_info")

    async def _get_min_notional_usd(self) -> float:
        """MIN_NOTIONAL символа в USD (0 = неизвестно/не задан)."""
        if self._min_notional is None:
            try:
                await self._get_symbol_filters()
            except Exception as e:
                self.log.debug(f"[LIVE] minNotional fetch failed: {e}")
        return float(self._min_notional or 0.0)

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

    async def _place_market_split(
        self, side: str, qty: float, mode: str, fallback_price: float = 0.0
    ) -> Tuple[float, float, int]:
        """Отправляет market-ордер(ы) суммарным объёмом qty, разбивая на части не
        больше биржевого maxQty (ограничение на ОДИН ордер). Каждая часть обязана
        пройти биржевой MIN_NOTIONAL, иначе ордер отклонят (-4164) — поэтому
        «хвост» меньше минимума либо подрезается в предыдущую часть, либо
        отбрасывается. Возвращает (filled_qty, vwap, chunks). Если весь объём
        отправить не удалось, filled_qty < qty (вызывающий считает это клампом).
        """
        await self._get_symbol_filters()
        max_qty = self._order_max_qty()
        price_ref = fallback_price if fallback_price and fallback_price > 0 else 0.0
        min_notional = await self._get_min_notional_usd()
        min_qty = (min_notional / price_ref) if (price_ref > 0 and min_notional > 0) else 0.0
        remaining = qty
        filled = 0.0
        notional = 0.0
        chunks = 0
        while remaining > 1e-12 and chunks < 50:
            if not max_qty or max_qty <= 0 or remaining <= max_qty:
                if min_qty > 0 and remaining < min_qty:
                    # остаток меньше MIN_NOTIONAL — отправить нельзя, отбрасываем
                    self.log.warning(
                        f"[REVERSE] split remainder below minNotional — dropping | "
                        f"remaining={remaining} min_qty={min_qty} price_ref={price_ref}"
                    )
                    break
                chunk = remaining
            else:
                chunk = max_qty
                # не оставляем «хвост» меньше MIN_NOTIONAL — подрезаем текущую часть
                if min_qty > 0 and (remaining - chunk) < min_qty:
                    chunk = remaining - min_qty
                if chunk <= 0 or chunk > max_qty:
                    chunk = max_qty
            chunk = await self._adjust_qty(chunk, mode=mode)
            if chunk <= 0:
                break
            order = await self.client.futures_create_order(
                symbol=self.cfg.symbol, side=side, type=ORDER_TYPE_MARKET, quantity=chunk,
            )
            _audit("reverse_market_chunk", symbol=self.cfg.symbol, side=side,
                   qty=chunk, orderId=(order or {}).get("orderId"))
            fp = await self._get_fill_price(order, fallback_price)
            if fp and fp > 0:
                notional += chunk * fp
            filled += chunk
            chunks += 1
            remaining = round(remaining - chunk, 12)
        vwap = (notional / filled) if filled > 0 and notional > 0 else 0.0
        return filled, vwap, chunks

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
                self.log.warning(
                    f"[LIVE] Could not fetch position info: {type(e).__name__}: {e!r}"
                )
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
                    try:
                        free = float(asset.get("availableBalance") or 0.0)
                    except (TypeError, ValueError):
                        free = 0.0
                    reset_ban_state()
                    cache.update(value=balance, free=free, ts=now, mode=mode)
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

    async def get_free_balance(self, mode: Optional[str] = None) -> float:
        """Свободная маржа (availableBalance) в USDT для live.

        Это депозит БЕЗ уже занятой под открытые позиции маржи: именно от него
        берётся position_size_pct. Для не-live возвращаем paper_balance.

        Использует тот же кэш, что и get_balance (availableBalance приходит в
        том же ответе futures_account_balance), поэтому лишнего REST-вызова в
        пределах одного цикла нет.
        """
        if mode is None:
            mode = self.cfg.mode
        if mode != "live":
            return self.cfg.paper_balance

        now = time.monotonic()
        cache = self._balance_cache
        if (cache.get("free") is not None and cache["mode"] == mode
                and (now - cache["ts"]) < BALANCE_CACHE_TTL_SEC):
            return float(cache["free"])

        # Освежаем через get_balance: он заполняет и value, и free.
        await self.get_balance(mode)
        if cache.get("free") is not None and cache["mode"] == mode:
            return float(cache["free"])
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
        self, direction: str, sl_price: float, qty: float = 0.0,
        exact_trigger: Optional[float] = None,
    ) -> Optional[int]:
        """Ставит широкий биржевой STOP_MARKET (closePosition=true) как safety-net.

        trigger = LONG: sl_price * (1 - pct/100), SHORT: sl_price * (1 + pct/100),
        где pct = cfg.exchange_sl_backstop_pct, округлённый по tickSize символа.
        Если задан exact_trigger > 0 — ставится ровно по нему (без ±pct).
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
            use_exact = exact_trigger is not None and exact_trigger > 0
            if direction == "LONG":
                trigger_raw = float(exact_trigger) if use_exact else sl_price * (1 - pct / 100)
                side = SIDE_SELL
                key = "long"
            else:
                trigger_raw = float(exact_trigger) if use_exact else sl_price * (1 + pct / 100)
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
                f"trigger={trigger_price} sl={sl_price} pct={pct}% exact={use_exact} "
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
        _audit("tp_limit", symbol=self.cfg.symbol, side=side, price=price, qty=qty)

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

    def _reverse_fee_params(self) -> Tuple[float, float, float]:
        """(f_in, f_out, p) для unified reverse-сайзинга в ДОЛЯХ (не %).

        f_in  — комиссия входа новой ноги: ордер market → taker;
        f_out — комиссия выхода: КОНСЕРВАТИВНО taker, потому что плановый
                TP-лимит может не исполниться/истечь и позиция закроется
                рыночным (reduceOnly) ордером;
        p     — целевой профит сверх безубытка как доля от notional выхода.
        """
        taker = float(getattr(self.cfg, "taker_fee_pct", 0.05) or 0.0) / 100.0
        p = float(getattr(self.cfg, "reverse_profit_pct", 0.1) or 0.0) / 100.0
        return taker, taker, p

    async def cycle_loss_cap(
        self, *, new_dir: str, net_qty: float, entry: float,
        net_realized: float, virtual_sl: float, pct: float,
        ref_deposit: Optional[float] = None,
    ) -> Optional[dict]:
        """Прототип: цена, при которой убыток ЦИКЛА = pct% реф-депозита.

        Считает PnL всего цикла (реализованный net_realized + нереализованный
        новой нетто-ноги минус оценочные комиссии входа/выхода) и находит цену,
        где он равен -pct% от реф-депозита. Триггер клэмпится так, чтобы НИКОГДА
        не оказаться раньше виртуального SL (иначе биржевой стоп порвал бы реверс).

        Возвращает dict либо None:
          ref         — использованный реф-депозит (USD)
          p_cap       — «сырая» цена убытка pct% цикла
          trigger     — фактический триггер для STOP_MARKET (клэмпнутый)
          clamped     — True, если p_cap пришлось отодвинуть шире виртуального SL
          worst_pct   — фактический worst-case убыток (%) при trigger
          attainable  — достижим ли target pct% (worst_pct <= pct)
        """
        try:
            f_in, f_out, _p = self._reverse_fee_params()
            q = float(net_qty) if new_dir == "LONG" else -float(net_qty)
            if not entry or entry <= 0 or abs(q) <= 0:
                return None
            ref = float(ref_deposit) if (ref_deposit and ref_deposit > 0) else 0.0
            if ref <= 0:
                ref = await self._get_equity()
            if ref <= 0:
                try:
                    ref = await self.get_balance("live")
                except Exception:
                    ref = 0.0
            if ref <= 0:
                return None

            x = abs(float(pct)) / 100.0
            aq = abs(q)
            fee_in = f_in * aq * entry
            # cycle_pnl(P) = net_realized + q*(P-entry) - fee_in - f_out*aq*P
            denom = q - f_out * aq
            if abs(denom) < 1e-12:
                return None
            p_cap_raw = (-x * ref - float(net_realized) + q * entry + fee_in) / denom

            sl = float(virtual_sl) if (virtual_sl and virtual_sl > 0) else 0.0
            pct_bs = float(getattr(self.cfg, "exchange_sl_backstop_pct", 2.0) or 0.0)
            if sl > 0 and new_dir == "LONG":
                gap = sl * (1 - pct_bs / 100.0)
                if p_cap_raw < sl:
                    trigger, clamped = max(p_cap_raw, gap), False
                else:
                    trigger, clamped = gap, True
            elif sl > 0:
                gap = sl * (1 + pct_bs / 100.0)
                if p_cap_raw > sl:
                    trigger, clamped = min(p_cap_raw, gap), False
                else:
                    trigger, clamped = gap, True
            else:
                trigger, clamped = p_cap_raw, False

            def _cycle_at(P: float) -> float:
                return float(net_realized) + q * (P - entry) - fee_in - f_out * aq * P

            worst = _cycle_at(trigger)
            worst_pct = (-worst) / ref * 100.0
            return {
                "ref": ref,
                "p_cap": p_cap_raw,
                "trigger": trigger,
                "clamped": clamped,
                "worst_pct": worst_pct,
                "attainable": worst_pct <= abs(float(pct)) + 1e-6,
            }
        except Exception as e:
            try:
                self.log.debug(f"[LOSSCAP] compute failed: {e}")
            except Exception:
                pass
            return None

    def _reverse_sizing(
        self, P: float, N: float, net_realized: float, U_eff: float, new_dir: str,
        w_pct: Optional[float] = None,
    ) -> Optional[Tuple[float, float, float, float, float, float, float]]:
        """Единый fee/profit-aware сайзинг реверса (1-й reverse и шаги цепочки).

        Подбирает совокупное знаковое нетто S_total (и новое плечо q = S_total - N)
        так, чтобы на T весь цикл давал профит p*|S_total|*T с учётом комиссий:

            w  = reverse_breakeven_pct/100 + reverse_fee_buffer_pct/100
            T  = P*(1+w)  (новое плечо LONG, N<0) | P*(1-w) (SHORT, N>0)
            f_in  = taker_fee_pct/100
            f_out = taker_fee_pct/100   (консервативно taker, см. _reverse_fee_params)
            p     = reverse_profit_pct/100
            |S_total| = [ -(net_realized + U_eff) + f_in*|N|*P ]
                        / [ |T-P| - f_in*P - (f_out+p)*T ]
            S_total = sign(new_leg) * |S_total|,  q = S_total - N

        U_eff = N*(P-A) считается вызывающим (A — entryPrice книги, fallback на
        биржевой unRealizedProfit). Возвращает (T, S_total, q, f_in, f_out, p, w)
        либо None, если знаменатель <= 0 или |S_total| <= 0: сайзинг невозможен,
        caller финализирует/принудительно закрывает как сегодня.
        """
        base_w = (
            float(w_pct) if w_pct is not None
            else float(getattr(self.cfg, "reverse_breakeven_pct", 0.5) or 0.0)
        )
        w = (
            base_w
            + float(getattr(self.cfg, "reverse_fee_buffer_pct", 0.0) or 0.0)
        ) / 100.0
        T = P * (1 + w) if new_dir == "LONG" else P * (1 - w)
        f_in, f_out, p = self._reverse_fee_params()
        denom = abs(T - P) - f_in * P - (f_out + p) * T
        if P <= 0 or denom <= 0:
            self.log.warning(
                f"[REVERSE] sizing aborted | denominator={denom} P={P} T={T} "
                f"N={N} net_realized={net_realized} U_eff={U_eff} "
                f"f_in={f_in} f_out={f_out} p={p} w={w} — no new leg"
            )
            return None
        s_abs = (-(net_realized + U_eff) + f_in * abs(N) * P) / denom
        if s_abs <= 0:
            self.log.warning(
                f"[REVERSE] sizing aborted | |S_total|={s_abs} denominator={denom} "
                f"P={P} T={T} N={N} net_realized={net_realized} U_eff={U_eff} "
                f"f_in={f_in} f_out={f_out} p={p} w={w} — no new leg"
            )
            return None
        S_total = s_abs if new_dir == "LONG" else -s_abs
        q = S_total - N
        return T, S_total, q, f_in, f_out, p, w

    async def open_reverse_position(
        self,
        original_direction: str,
        original_entry: float,
        original_qty: float,
        sl_price: float,
        mode: Optional[str] = None,
        net_position: Optional[float] = None,
        net_realized: Optional[float] = None,
        step: Optional[int] = None,
        unrealized: Optional[float] = None,
        position_entry: Optional[float] = None,
    ) -> Optional[Tuple[float, float, float]]:
        """
        Разворот при срабатывании SL.

        net_position / net_realized / unrealized / position_entry — данные книги
        цикла (необязательные). Когда они доступны, используется ЕДИНЫЙ
        fee/profit-aware сайзинг и для 1-го reverse, и для шагов цепочки
        (см. _reverse_sizing / _open_reverse_position_chain): сайзинг считается
        от всего нетто N, уже-реализованного net_realized цикла и НЕреализованного
        U_eff книги на триггере P, чтобы вывести ВСЮ книгу в целевой профит
        p*|S_total|*T на T. Для 1-го reverse net_realized=0, N — знаковая исходная
        позиция, U_eff — её unrealized на триггере; step=None (cap цепочки не
        применяется). unrealized — unRealizedProfit с биржи, position_entry —
        entryPrice книги (для консистентного U_eff = N*(P-A)).
        При step >= cfg.reverse_chain_max новый шаг не открывается (возврат None —
        вызывающий принудительно закрывает позицию). Если данные книги недоступны
        (net_position/net_realized не переданы) — legacy per-leg формула
        (обратная совместимость, в лог пишется path=legacy).

        Исходная позиция НЕ закрывается отдельным ордером. В обратную сторону
        отправляется рыночный ордер, оставляющий удерживаемый (хедж) объём.
        Объём хеджа, при котором обе ноги выходят в ноль на плановой цене
        P3_plan, считается от ожидаемого филла S по формуле

            held_plan = Qo * |E - S| / |S - P3_plan|,   P3_plan = S * (1 ∓ pct)

        где E = original_entry, S = sl_price, Qo = original_qty,
        pct = reverse_breakeven_pct / 100. Числитель — реализованный на
        развороте убыток исходной ноги, поэтому формула даёт истинный
        безубыток (старая Qo*|E-P3_plan|/|S-P3_plan| была больше ровно на Qo).
        Объём отправки send = held_plan + Qo (закрывает Qo и открывает
        held_plan нетто), но не больше биржевого maxQty (LOT_SIZE /
        MARKET_LOT_SIZE).

        После исполнения обратного ордера фактическая цена E_rev
        (avgPrice/fills) и фактический хедж Qh (реальный нетто-объём обратной
        позиции, fallback send - Qo) дают точную безубыточную цену

            reverse SHORT: P* = E_rev - Qo * |E - E_rev| / Qh
            reverse LONG : P* = E_rev + Qo * |E - E_rev| / Qh

        P* округляется по tickSize и проверяется по геометрии (SHORT: P* <
        E_rev, LONG: P* > E_rev); при нарушении — сдвиг на тик в прибыльную
        сторону, иначе биржевой TP не выставляется. TP обратной позиции
        ставится ровно на P* на весь оставшийся нетто-объём.

        Возвращает (entry_price, held_qty, tp_price), где tp_price = P*.
        """
        if mode is None:
            mode = self.cfg.mode

        # Единый fee/profit-aware сайзинг используется И для 1-го reverse, И для
        # шагов цепочки, когда доступны данные книги цикла. Для 1-го reverse
        # step не задан → cap цепочки не применяется (step=0 в chain-методе).
        if net_position is not None and net_realized is not None:
            if step is not None:
                try:
                    max_steps = int(getattr(self.cfg, "reverse_chain_max", 10) or 0)
                except (TypeError, ValueError):
                    max_steps = 0
                # max_steps <= 0 = без лимита шагов (добавляем ногу сколько нужно).
                if max_steps > 0 and step >= max_steps:
                    self.log.warning(
                        f"[REVERSE] chain limit reached (step={step}) — no new leg"
                    )
                    return None
            return await self._open_reverse_position_chain(
                original_direction=original_direction,
                original_entry=original_entry,
                original_qty=original_qty,
                sl_price=sl_price,
                mode=mode,
                net_position=net_position,
                net_realized=net_realized,
                step=int(step or 0),
                unrealized=unrealized,
                position_entry=position_entry,
            )
        # Fallback: unified-входы недоступны → legacy per-leg формула.
        # ВАЖНО: для шагов ЦЕПОЧКИ (step>0) legacy недопустима — она считает
        # безубыток только текущей ноги и игнорирует уже накопленный убыток
        # цикла, из-за чего открывается крошечный хедж и цикл закрывается в
        # минус на TP. В таком случае ногу не открываем, caller финализирует
        # (принудительное закрытие / повтор).
        if step is not None and int(step) > 0:
            self.log.error(
                f"[REVERSE] chain step={step}: unified inputs unavailable "
                f"(net_position={net_position} net_realized={net_realized}) — "
                f"refusing legacy under-sized hedge"
            )
            return None
        self.log.warning(
            f"[REVERSE] unified inputs unavailable (step={step} "
            f"net_position={net_position} net_realized={net_realized}) — "
            f"using legacy per-leg sizing"
        )

        reverse_dir = "SHORT" if original_direction == "LONG" else "LONG"

        pct = float(getattr(self.cfg, "reverse_breakeven_pct", 0.5) or 0.5) / 100

        # Планировочный P3 от виртуального SL — нужен только для оценки объёма
        # отправки. Это ИСТИННЫЙ безубыток для ожидаемого филла на S: исходная
        # нога закрывается неттингом по S (реализованный убыток Qo*|E-S|), а
        # хедж должен вернуть его к P3_plan, поэтому
        #   held_plan = Qo * |E - S| / |S - P3_plan|.
        # Старая формула Qo*|E-P3_plan|/|S-P3_plan| давала ровно held_plan + Qo
        # и пересайзила отправку ровно на Qo.
        if original_direction == "LONG":
            p3_plan = sl_price * (1 - pct)
        else:
            p3_plan = sl_price * (1 + pct)

        held_qty = await self._adjust_qty(
            abs(original_entry - sl_price) / abs(sl_price - p3_plan) * original_qty, mode=mode
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
        f_in, f_out, p = self._reverse_fee_params()
        self.log.info(
            f"[REVERSE] sizing | dir={original_direction} E={original_entry} S={sl_price} "
            f"P3_plan={p3_plan} pct={pct * 100}% qty={original_qty} held={held_qty} "
            f"send={send_qty} mult={mult} N=None net_realized=None U_eff=None "
            f"A={original_entry} P={sl_price} T={p3_plan} |S_total|={held_qty} "
            f"q={send_qty} f_in={f_in} f_out={f_out} p={p} path=legacy"
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

        # Фактический хедж после разворота: реальный нетто-объём обратной
        # позиции; если биржа недоступна (<=0) — расчётный send - Qo.
        fallback_held = send_qty - original_qty
        real_qty = await self._get_real_position_qty(reverse_dir)
        if real_qty > 0:
            qh = await self._adjust_qty(real_qty, mode=mode)
        else:
            qh = await self._adjust_qty(fallback_held, mode=mode)

        # Точка безубыточности объединённого PnL обеих ног по ФАКТИЧЕСКИМ
        # E_rev и Qh. Исходная нога сведена неттингом по E_rev (реализованный
        # убыток Qo*|E - E_rev|), поэтому:
        #   reverse SHORT: P* = E_rev - Qo*|E - E_rev| / Qh
        #   reverse LONG : P* = E_rev + Qo*|E - E_rev| / Qh
        # Это заменяет старый pct-ориентир P3 = E_rev*(1 ∓ pct) как TP.
        if reverse_dir == "SHORT":
            p3_ref = entry_price * (1 - pct)
        else:
            p3_ref = entry_price * (1 + pct)

        p_star = None
        tp_price = None
        tp_ok = False
        # FIX 2: хеджировать нечего — новую отслеживаемую ногу НЕ создаём
        # (held_qty=0 в возврате). Иначе caller открывал фантомную reverse-ногу
        # и перефинализировал строку по чужим биржевым филлам.
        no_hedge = False
        if qh <= 0:
            self.log.warning(
                f"[REVERSE] nothing to hedge (Qh={qh}) — no new leg | "
                f"dir={reverse_dir} E_rev={entry_price} E={original_entry} Qo={original_qty}"
            )
            no_hedge = True
            tp_price = await self._adjust_price(p3_ref, mode=mode)
        elif abs(original_entry - entry_price) <= 0:
            self.log.warning(
                f"[REVERSE] |E - E_rev| <= 0 — nothing to hedge, no new leg | "
                f"dir={reverse_dir} E={original_entry} E_rev={entry_price}"
            )
            no_hedge = True
            tp_price = await self._adjust_price(p3_ref, mode=mode)
        else:
            move = original_qty * abs(original_entry - entry_price) / qh
            p_star = entry_price - move if reverse_dir == "SHORT" else entry_price + move
            tp_price = await self._adjust_price(p_star, mode=mode)

            # Геометрия: для SHORT TP обязан быть ниже E_rev, для LONG — выше.
            # Округление по tickSize может схлопнуть его на/за E_rev — тогда
            # сдвигаем минимум на один тик в прибыльную сторону. Если и это
            # невозможно, TP не отправляем (маркет-исполнение недопустимо).
            def _correct_side(tp: float) -> bool:
                return tp < entry_price if reverse_dir == "SHORT" else tp > entry_price

            if not _correct_side(tp_price):
                tick = self._tick_size if mode == "live" else None
                if tick and tick > 0:
                    nudged = tp_price - tick if reverse_dir == "SHORT" else tp_price + tick
                    tp_price = await self._adjust_price(nudged, mode=mode)
                    self.log.warning(
                        f"[REVERSE] P* rounded to wrong side → nudged | dir={reverse_dir} "
                        f"E_rev={entry_price} raw_p_star={p_star} nudged_p_star={tp_price} tick={tick}"
                    )
            tp_ok = _correct_side(tp_price)
            if not tp_ok:
                self.log.error(
                    f"[REVERSE] P* on wrong side of actual fill — aborting TP | "
                    f"dir={reverse_dir} E_rev={entry_price} p_star={p_star} "
                    f"p_star_adj={tp_price} Qo={original_qty} Qh={qh}"
                )

            dist_pct = abs(entry_price - tp_price) / entry_price * 100 if entry_price else 0.0
            self.log.info(
                f"[REVERSE] be | E={original_entry} E_rev={entry_price} Qo={original_qty} "
                f"Qh={qh} P3={p3_ref} P*={tp_price} pct={pct * 100}% dist_pct={dist_pct} "
                f"N=None net_realized=None U_eff=None A={original_entry} P={sl_price} "
                f"T={p3_ref} |S_total|={qh} q={original_qty + qh} "
                f"f_in={f_in} f_out={f_out} p={p} path=legacy"
            )
            self.log.info(
                f"[REVERSE] hedge | planned={held_qty} actual={qh} "
                f"E={original_entry} E_rev={entry_price} S={sl_price} P*={tp_price}"
            )

        # TP закрывает ВСЮ оставшуюся позицию, чтобы не осталось небезубыточного
        # остатка для дампа по рынку. Берём фактический нетто-объём после
        # разворота; если он недоступен (<=0), падаем на расчётный send - Qo.
        step = self._step_size if (mode == "live" and self._step_size) else 0.0
        if real_qty > 0:
            adjusted_real = await self._adjust_qty(real_qty, mode=mode)
            tp_qty = min(adjusted_real, real_qty)
        else:
            tp_qty = qh
        if step > 0 and abs(tp_qty - fallback_held) > step:
            self.log.warning(
                f"[REVERSE] TP qty != send-Qo | tp_qty={tp_qty} "
                f"hedge_actual={qh} send_minus_orig={fallback_held} real_qty={real_qty}"
            )
        held_qty = tp_qty
        if no_hedge:
            # Сигнал caller'у «нога не добавлена» (FIX 2): не открывать
            # reverse-позицию в трекере и не перефинализировать строку.
            held_qty = 0.0

        # TP обратной позиции ровно на P* (или не ставим, если геометрия
        # невозможна/хеджировать нечего).
        if mode == "live" and held_qty > 0 and tp_ok and tp_price is not None:
            try:
                await self._place_tp_limit(reverse_dir, tp_price, held_qty)
                self.log.info(f"[REVERSE] TP placed | {reverse_dir} tp={tp_price} qty={held_qty}")
            except Exception as e:
                self.log.error(f"[REVERSE] Failed to place TP: {e}", exc_info=True)

        return entry_price, held_qty, tp_price

    async def _open_reverse_position_chain(
        self,
        original_direction: str,
        original_entry: float,
        original_qty: float,
        sl_price: float,
        mode: str,
        net_position: float,
        net_realized: float,
        step: int = 0,
        unrealized: Optional[float] = None,
        position_entry: Optional[float] = None,
    ) -> Optional[Tuple[float, float, float]]:
        """Шаг reverse-ЦЕПОЧКИ (и 1-й reverse при step=0): выводит книгу в профит.

        P — текущая цена триггера (виртуальный SL обратной ноги; для 1-го reverse
        step=0), N — знаковое нетто на бирже (positionAmt), net_realized — уже
        реализованный net-PnL цикла (для 1-го reverse = 0), U_eff — НЕреализованный
        PnL открытой книги на P = N*(P-A) при известном входе A, иначе unrealized
        с биржи. Новое плечо q противоположно N, а совокупная позиция S_total
        подбирается единой формулой (см. _reverse_sizing) так, чтобы на
        T = P*(1±w) весь цикл давал профит p*|S_total|*T с учётом комиссий:

            w         = reverse_breakeven_pct/100 + reverse_fee_buffer_pct/100
            T         = P*(1+w)  (новое плечо LONG) | P*(1-w) (новое плечо SHORT)
            f_in      = taker_fee_pct/100          # новая нога — market
            f_out     = taker_fee_pct/100          # консервативно taker-выход
            p         = reverse_profit_pct/100
            |S_total| = [ -(net_realized + U_eff) + f_in*|N|*P ]
                        / [ |T-P| - f_in*P - (f_out+p)*T ]
            S_total   = sign(new_leg) * |S_total|  # знаковое нетто ПОСЛЕ
            q         = S_total - N                # знаковый объём нового плеча

        FIX 1: без U_eff сайзинг не учитывал текущий убыток открытой позиции и
        цикл закрывался не в ноль, а в ≈ -1% номинала.

        Валидация: sign(q) == -sign(N) (либо N == 0), |q| > 0; |q| клампится по
        биржевому maxQty (при клампе точный ноль недостижим — warning). Возвращает
        (entry_price, held_qty, tp_price), где held_qty = |нетто после входа|, а
        TP (reduceOnly LIMIT) ставится на T на весь этот объём. held_qty == 0 —
        особая метка «нога не добавлена» (nothing to hedge / книга уже выходит в
        ноль на T): caller не должен открывать reverse в трекере.
        """
        try:
            P = float(sl_price or 0.0)
            N = float(net_position or 0.0)
            nr = float(net_realized or 0.0)
        except (TypeError, ValueError):
            self.log.warning(
                f"[REVERSE] chain invalid inputs | sl={sl_price} "
                f"N={net_position} net_realized={net_realized} — no new leg"
            )
            return None
        if P <= 0:
            self.log.warning(f"[REVERSE] chain invalid trigger P={P} — no new leg")
            return None
        try:
            U = float(unrealized) if unrealized is not None else 0.0
        except (TypeError, ValueError):
            U = 0.0
        try:
            A = float(position_entry) if position_entry is not None else 0.0
        except (TypeError, ValueError):
            A = 0.0

        # Направление нового плеча — противоположно знаку N (назад к исходной
        # стороне). При N == 0 падаем на противоположное original_direction.
        if N > 0:
            new_dir = "SHORT"
        elif N < 0:
            new_dir = "LONG"
        else:
            new_dir = "SHORT" if original_direction == "LONG" else "LONG"

        # step == 0 → это 1-й reverse (собственный аудит-лог [REVERSE] sizing/be
        # в дополнение к [REVERSE] chain).
        is_first = (step == 0)
        # unrealized открытой книги. Берём КОНСЕРВАТИВНУЮ оценку убытка: биржевой
        # unrealizedProfit может прийти нулевым/устаревшим (особенно на testnet,
        # где один аккаунт делят десятки ботов) — тогда сайзинг считает убыток
        # цикла почти нулевым, хедж получается крошечным и TP-выход НЕ закрывает
        # цикл в ноль (наблюдалось: цикл −3$ при хедже, посчитанном на −0.45$).
        # Поэтому если рассчитанный N*(P−A) того же знака и по модулю больше
        # биржевого U — доверяем ему; биржевой U берём, только когда он учитывает
        # реальный убыток не меньше расчётного (например, из-за проскальзывания).
        u_price = (N * (P - A)) if A > 0 else None
        if u_price is not None:
            if (abs(U) > 1e-9 and (U < 0) == (u_price < 0) and abs(U) >= abs(u_price)):
                U_eff = U
            else:
                U_eff = u_price
        else:
            U_eff = U
        # Ступенчатая цель по шагу цепочки (вариант B): 0.5, 0.5, 1, 2, 4, 8 % …
        w_step = reverse_w_pct_for_step(self.cfg, step)
        # Единый fee/profit-aware сайзинг: используется и для 1-го reverse, и для
        # шагов цепочки. None → сайзинг невозможен (denominator/|S_total| <= 0).
        sizing = self._reverse_sizing(P, N, nr, U_eff, new_dir, w_pct=w_step)
        if sizing is None:
            return None
        T, S_total, q, f_in, f_out, p, w = sizing
        # Итоговая цель TP. Если после фактического филла делается top-up до
        # требуемого нетто — цель пересчитывается от ФАКТИЧЕСКОГО входа (T2),
        # иначе остаётся плановая T от триггера P. Раньше T2 терялся и TP
        # ставился по цене, не соответствующей реальному нетто (недобор).
        T_target = T
        # Edge case: требуемое нетто того же знака, что и текущее, и не больше
        # его по модулю — книга уже выводится в ноль на T без новой ноги.
        # Не отправляем ордер, только ставим reduceOnly TP на |N| по T.
        if (N != 0 and (S_total > 0) == (N > 0) and abs(S_total) <= abs(N)):
            T_adj = await self._adjust_price(T, mode=mode)
            pos_dir = "LONG" if N > 0 else "SHORT"
            self.log.info(
                f"[REVERSE] chain | no new leg needed (book already reaches zero) "
                f"| step={step} N={N} net_realized={nr} U_eff={U_eff} A={A} "
                f"P={P} T={T_adj} |S_total|={abs(S_total)} q={q} "
                f"f_in={f_in} f_out={f_out} p={p} w={w}"
            )
            if mode == "live":
                try:
                    await self._get_symbol_filters()
                    tp_qty = await self._adjust_qty(abs(N), mode=mode)
                    if tp_qty > 0:
                        await self._place_tp_limit(pos_dir, T_adj, tp_qty)
                        self.log.info(
                            f"[REVERSE] chain TP (no new leg) placed | {pos_dir} "
                            f"tp={T_adj} qty={tp_qty}"
                        )
                except Exception as e:
                    self.log.error(
                        f"[REVERSE] chain no-leg TP placement failed: {e}",
                        exc_info=True,
                    )
            return P, 0.0, T_adj
        # S_total обязан быть на стороне нового плеча (иначе геометрия цикла
        # невыполнима: убыток/прибыль цикла не выводится в ноль этой стороной).
        if (S_total > 0) != (new_dir == "LONG"):
            self.log.warning(
                f"[REVERSE] chain geometry mismatch | dir={new_dir} N={N} "
                f"net_realized={nr} S_total={S_total} — no new leg"
            )
            return None

        # q = S_total - N уже посчитан unified-сайзингом.
        if abs(q) <= 0:
            self.log.warning(
                f"[REVERSE] chain zero q | step={step} N={N} S_total={S_total}"
            )
            return None
        # q обязано быть противоположно N (закрываем/переворачиваем нетто).
        if N != 0 and ((q > 0) == (N > 0)):
            self.log.warning(
                f"[REVERSE] chain sign mismatch | N={N} q={q} "
                f"(sign(q) != -sign(N)) — no new leg"
            )
            return None

        qty = abs(q)
        capped = False
        # Потолок нотионала (конфиг/%equity/биржевой брекет) — ограничение на ВЕСЬ
        # нетто, его не обходим. А биржевой maxQty на ОДИН ордер обходим разбивкой
        # (см. _place_market_split ниже), иначе хедж режется и цикл не выходит в ноль.
        if mode == "live":
            cap_usd = await self._notional_cap_usd()
            if cap_usd > 0 and P and P > 0:
                max_qty_cap = cap_usd / P
                if qty > max_qty_cap:
                    self.log.warning(
                        f"[REVERSE] chain qty capped by notional cap | requested={qty} "
                        f"cap_qty={max_qty_cap} notional_cap=${cap_usd:.2f}"
                    )
                    qty = max_qty_cap
                    capped = True
        qty = await self._adjust_qty(qty, mode=mode)
        if qty <= 0:
            self.log.warning(f"[REVERSE] chain qty={qty} <= 0 — no new leg")
            return None

        signed_qty = qty if q > 0 else -qty

        # MARKET-ордер нового плеча. Если объём больше биржевого maxQty на один
        # ордер — шлём несколькими частями суммарно на весь объём.
        side = _direction_to_side("LONG" if signed_qty > 0 else "SHORT")
        entry_price = P
        if mode == "live":
            try:
                await self._set_leverage()
                filled, vwap, chunks = await self._place_market_split(side, qty, mode, fallback_price=P)
                if filled <= 0:
                    raise RuntimeError("market split returned no fill")
                if filled < qty - 1e-9:
                    # не удалось отправить весь объём — цикл в ноль не выведется
                    capped = True
                if chunks > 1:
                    self.log.warning(
                        f"[REVERSE] chain market split | side={side} requested={qty} "
                        f"filled={filled} chunks={chunks} vwap={vwap:.8f}"
                    )
            except Exception as e:
                self.log.error(
                    f"[REVERSE] chain market order failed | side={side} qty={qty}: {e}",
                    exc_info=True,
                )
                return None
            # Вход изменил позицию/баланс — кэши невалидны.
            self._invalidate_caches()
            if vwap and vwap > 0:
                entry_price = vwap
            signed_qty = filled if q > 0 else -filled
            qty = filled
        else:
            self.log.info(
                f"[PAPER] Reverse chain send {side} qty={qty} @ {entry_price}"
            )
        if entry_price is None or entry_price <= 0:
            self.log.error(
                f"[REVERSE] chain invalid fill price={entry_price} — aborting TP"
            )
            return None

        # Фактическое нетто после ордера. Без клампа = S_total; при клампе
        # используем реально достижимый объём (цикл в ноль не выводится).
        if capped:
            actual_net = N + signed_qty
        else:
            actual_net = S_total
        tp_qty = await self._adjust_qty(abs(actual_net), mode=mode)
        if tp_qty <= 0:
            self.log.warning(
                f"[REVERSE] chain TP qty={tp_qty} <= 0 | actual_net={actual_net}"
            )
            return None

        # Добор хеджа: если по ФАКТИЧЕСКОМУ входу требуемое нетто больше
        # достигнутого (проскальзывание между триггером и филлом), добавляем
        # недостающий объём — чтобы TP хеджа покрывал убыток первой ноги.
        # Ограничения: live, не кламплено, в пределах maxQty и потолка нотионала.
        if mode == "live" and not capped:
            try:
                _Uf = N * (entry_price - A) if A > 0 else U
                resize2 = self._reverse_sizing(entry_price, N, nr, _Uf, new_dir, w_pct=w_step)
                if resize2 is not None:
                    T2 = float(resize2[0])
                    S_req = float(resize2[1])
                    if (S_req > 0) == (new_dir == "LONG"):
                        need = abs(S_req) - abs(actual_net)
                        add_qty = await self._adjust_qty(need, mode=mode) if need > 0 else 0.0
                        cap_usd2 = await self._notional_cap_usd()
                        if cap_usd2 > 0 and entry_price > 0:
                            room = cap_usd2 - abs(actual_net) * entry_price
                            add_qty = 0.0 if room <= 0 else min(add_qty, room / entry_price)
                            add_qty = await self._adjust_qty(add_qty, mode=mode)
                        if add_qty > 0:
                            side2 = _direction_to_side(new_dir)
                            add_filled, add_vwap, add_chunks = await self._place_market_split(
                                side2, add_qty, mode, fallback_price=entry_price
                            )
                            self._invalidate_caches()
                            if add_filled > 0:
                                fill2 = add_vwap if add_vwap and add_vwap > 0 else entry_price
                                prev = abs(actual_net)
                                tot = prev + add_filled
                                if tot > 0:
                                    entry_price = (entry_price * prev + fill2 * add_filled) / tot
                                actual_net = actual_net + (add_filled if new_dir == "LONG" else -add_filled)
                                tp_qty = await self._adjust_qty(abs(actual_net), mode=mode)
                                T_target = T2
                                T_adj = await self._adjust_price(T2, mode=mode)
                                self.log.warning(
                                    f"[REVERSE] top-up hedge added | dir={new_dir} add={add_filled} "
                                    f"chunks={add_chunks} net={actual_net} tp={T_adj} (to cover first-leg loss)"
                                )
            except Exception as e:
                self.log.warning(f"[REVERSE] top-up skipped: {e}")

        T_adj = await self._adjust_price(T_target, mode=mode)

        # Геометрия: LONG TP выше входа, SHORT — ниже. Округление по tickSize
        # может схлопнуть на/за вход — сдвигаем на тик в прибыльную сторону.
        def _tp_correct(tp: float) -> bool:
            return tp > entry_price if new_dir == "LONG" else tp < entry_price

        tp_ok = _tp_correct(T_adj)
        if not tp_ok:
            # Фактический филл ушёл от триггера P (проскальзывание/гэп): цель
            # T = P*(1±w) оказалась по убыточную сторону от реального входа.
            # Оставлять её нельзя — виртуальный TP сработает сразу при открытии
            # и закроет хедж по цене входа (цикл не выйдет в безубыток). Поэтому
            # пересчитываем цель от ФАКТИЧЕСКОГО входа.
            U_at_fill = N * (entry_price - A) if A > 0 else U
            resized = self._reverse_sizing(entry_price, N, nr, U_at_fill, new_dir, w_pct=w_step)
            if resized is not None:
                T_recalc = await self._adjust_price(resized[0], mode=mode)
                if _tp_correct(T_recalc):
                    self.log.warning(
                        f"[REVERSE] chain T recalculated from actual fill | dir={new_dir} "
                        f"entry={entry_price} P={P} old_T={T_adj} new_T={T_recalc}"
                    )
                    T_adj = T_recalc
                    tp_ok = True
        if not tp_ok:
            tick = self._tick_size if mode == "live" else None
            if tick and tick > 0:
                nudged = T_adj + tick if new_dir == "LONG" else T_adj - tick
                T_adj = await self._adjust_price(nudged, mode=mode)
            tp_ok = _tp_correct(T_adj)
        if not tp_ok:
            self.log.error(
                f"[REVERSE] chain T on wrong side of fill — TP skipped | "
                f"dir={new_dir} entry={entry_price} T={T_adj} N={N} S_total={S_total}"
            )

        # Наблюдаемость: ожидаемый PnL всего цикла, если TP реально исполнится на
        # T_adj. Если он отрицательный — хедж недобрал (cap/сайзинг) и цикл
        # закроется в минус даже как REVERSE_BE: логируем числа для диагностики.
        try:
            _exp_cycle = (
                nr + N * (entry_price - A)
                + actual_net * (T_adj - entry_price)
                - f_in * abs(q) * entry_price
                - f_out * tp_qty * T_adj
            )
            if _exp_cycle < -1e-9:
                self.log.warning(
                    f"[REVERSE] chain TP leaves cycle negative | step={step} "
                    f"expected={_exp_cycle:+.4f} T={T_adj} entry={entry_price} "
                    f"net={actual_net} q={q} nr={nr} N={N} A={A} capped={capped}"
                )
            else:
                self.log.info(
                    f"[REVERSE] chain TP expected_cycle_pnl={_exp_cycle:+.4f} "
                    f"step={step} T={T_adj}"
                )
        except Exception:
            pass

        mult = abs(q) / abs(N) if N else 0.0
        self.log.info(
            f"[REVERSE] chain | step={step} N={N} net_realized={nr} U_eff={U_eff} "
            f"A={A} P={P} T={T_adj} |S_total|={abs(S_total)} q={q} "
            f"f_in={f_in} f_out={f_out} p={p} w={w} mult={mult}"
        )
        if is_first:
            # 1-й reverse: сохраняем привычные аудит-строки [REVERSE] sizing/be,
            # добавляя в них unified-поля (fee/profit-aware сайзинг).
            self.log.info(
                f"[REVERSE] sizing | N={N} net_realized={nr} U_eff={U_eff} A={A} "
                f"P={P} T={T_adj} |S_total|={abs(S_total)} q={q} "
                f"f_in={f_in} f_out={f_out} p={p} w={w} mult={mult} path=unified"
            )
            self.log.info(
                f"[REVERSE] be | E={original_entry} E_rev={entry_price} "
                f"Qo={original_qty} Qh={tp_qty} P={P} T={T_adj} "
                f"N={N} net_realized={nr} U_eff={U_eff} A={A} "
                f"|S_total|={abs(S_total)} q={q} f_in={f_in} f_out={f_out} "
                f"p={p} w={w} mult={mult} path=unified"
            )

        if mode == "live" and tp_qty > 0 and tp_ok:
            try:
                await self._place_tp_limit(new_dir, T_adj, tp_qty)
                self.log.info(
                    f"[REVERSE] chain TP placed | {new_dir} tp={T_adj} qty={tp_qty}"
                )
            except Exception as e:
                self.log.error(f"[REVERSE] chain TP placement failed: {e}", exc_info=True)

        return entry_price, tp_qty, T_adj

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

        # position_size_pct: МАРЖА = % от СВОБОДНОГО депозита, позиция = маржа ×
        # плечо. Приоритет ниже position_size_usd: явный USD-размер перебивает
        # процент. Открытые позиции не учитываются — для live берётся
        # availableBalance (он уже без маржи под открытые позиции), для testnet
        # это paper_balance.
        #
        # Раньше ветка была ограничена BOT_ENV == "live", из-за чего в testnet
        # поле молча игнорировалось: массовое задание размера из дашборда
        # записывало значение, а боты продолжали считать размер по
        # fixed_risk_usd / risk_pct. Убираем гейт — семантика теперь одинаковая
        # в обоих окружениях. Размер действует только на НОВЫЕ позиции:
        # открытая позиция и её reverse-цепочка размер не меняют, он зафиксирован
        # при входе (см. sizing ниже).
        pct_margin = 0.0
        pct_free_balance = 0.0
        if not is_recovery and self.cfg.position_size_usd <= 0:
            try:
                pct = float(getattr(self.cfg, "position_size_pct", 0.0) or 0.0)
            except (TypeError, ValueError):
                pct = 0.0
            if pct > 0:
                pct_free_balance = await self.get_free_balance(mode)
                if pct_free_balance > 0:
                    margin = pct_free_balance * pct / 100
                    # Нижний порог нотионала: процент от малого свободного депозита
                    # не должен уходить ниже MIN_NOTIONAL биржи, иначе ордер отклонят.
                    min_notional = await self._get_min_notional_usd()
                    min_margin = (min_notional / self.cfg.leverage) if min_notional > 0 else 0.0
                    if min_margin > 0 and margin < min_margin:
                        if min_margin <= pct_free_balance:
                            margin = math.ceil(min_margin * 100) / 100
                            self.log.info(
                                f"[LIVE] position_size_pct below minNotional "
                                f"(floor=${min_notional:.2f}) — margin bumped to ${margin:.2f}"
                            )
                        else:
                            self.log.warning(
                                f"[LIVE] position_size_pct below minNotional "
                                f"(floor=${min_notional:.2f}) and free balance "
                                f"${pct_free_balance:.2f} too low — fallback to legacy sizing"
                            )
                            margin = 0.0
                    else:
                        margin = round(margin, 2)
                    pct_margin = margin if margin > 0 else 0.0
                else:
                    self.log.warning(
                        "[LIVE] position_size_pct set but free balance unavailable — "
                        "fallback to legacy sizing"
                    )

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
        elif self.cfg.position_size_usd > 0:
            # МАРЖА в USD (ручной объём из UI): позиция = margin * leverage.
            margin = self.cfg.position_size_usd
            raw_qty = (margin * self.cfg.leverage) / signal.entry_price
            self.log.info(
                f"[LIVE] Size by position_size_usd(margin) | margin=${margin:.2f} "
                f"leverage={self.cfg.leverage}x notional=~${margin * self.cfg.leverage:.2f} "
                f"entry={signal.entry_price:.6f} qty={raw_qty:.6f}"
            )
        elif pct_margin > 0:
            # МАРЖА = % от СВОБОДНОГО депозита (availableBalance): позиция = margin * leverage.
            margin = pct_margin
            raw_qty = (margin * self.cfg.leverage) / signal.entry_price
            self.log.info(
                f"[LIVE] Size by position_size_pct | free_margin=${pct_free_balance:.2f} "
                f"pct={self.cfg.position_size_pct}% margin=${margin:.2f} "
                f"leverage={self.cfg.leverage}x notional=~${margin * self.cfg.leverage:.2f} "
                f"entry={signal.entry_price:.6f} qty={raw_qty:.6f}"
            )
        elif _live_default_margin() > 0:
            # Глобальный дефолт live-окружения: маржа из LIVE_DEFAULT_MARGIN_USD.
            margin = _live_default_margin()
            raw_qty = (margin * self.cfg.leverage) / signal.entry_price
            self.log.info(
                f"[LIVE] Size by LIVE_DEFAULT_MARGIN_USD | margin=${margin:.2f} "
                f"leverage={self.cfg.leverage}x notional=~${margin * self.cfg.leverage:.2f} "
                f"entry={signal.entry_price:.6f} qty={raw_qty:.6f}"
            )
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
        # Потолок нотионала (конфиг / % от equity с нереализованным PnL / брекет биржи).
        if mode == "live" and raw_qty > 0 and signal.entry_price > 0:
            cap_usd = await self._notional_cap_usd()
            if cap_usd > 0:
                max_qty_cap = cap_usd / signal.entry_price
                if raw_qty > max_qty_cap:
                    self.log.info(
                        f"[CAP] qty clamped by notional cap | requested={raw_qty:.6f} "
                        f"capped={max_qty_cap:.6f} notional_cap=${cap_usd:.2f}"
                    )
                    raw_qty = max_qty_cap
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
            _audit("entry", symbol=self.cfg.symbol, direction=signal.direction, qty=qty, orderId=(order or {}).get("orderId"))
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
            _audit("close_market", symbol=self.cfg.symbol, direction=direction, qty=qty)
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

    async def _get_symbol_max_leverage(self) -> Optional[int]:
        """Максимально допустимое плечо символа из futures_leverage_bracket.

        Кэшируется на инстансе после первого успешного чтения. Бросает только
        ошибки самого REST-вызова — их обрабатывает _set_leverage.
        """
        if self._max_leverage_cache is not None:
            return self._max_leverage_cache
        data = await self.client.futures_leverage_bracket(symbol=self.cfg.symbol)
        max_lev = _extract_max_leverage(data)
        if max_lev is not None:
            self._max_leverage_cache = max_lev
        return max_lev

    async def _get_symbol_notional_cap(self) -> Optional[float]:
        """Биржевой потолок нотионала (USD) для текущего плеча символа."""
        if self._notional_cap_cache is not None:
            return self._notional_cap_cache
        try:
            leverage = int(self.cfg.leverage)
        except (TypeError, ValueError):
            leverage = 1
        try:
            data = await self.client.futures_leverage_bracket(symbol=self.cfg.symbol)
            cap = _extract_notional_cap(data, leverage)
            if cap is not None:
                self._notional_cap_cache = cap
            return cap
        except Exception as e:
            self.log.debug(f"[CAP] bracket notional fetch failed: {e}")
            return None

    async def _get_equity(self) -> float:
        """Equity счёта с учётом нереализованного PnL (totalMarginBalance)."""
        now = time.time()
        cache = self._equity_cache
        if cache["value"] is not None and (now - cache["ts"]) < 30.0:
            return float(cache["value"])
        try:
            acct = await self.client.futures_account()
            eq = float(acct.get("totalMarginBalance") or 0.0)
            if eq <= 0:
                eq = float(acct.get("totalWalletBalance") or 0.0) + float(acct.get("totalUnrealizedProfit") or 0.0)
            if eq > 0:
                cache.update(value=eq, ts=now)
                return eq
        except Exception as e:
            self.log.debug(f"[CAP] equity fetch failed: {e}")
        return float(cache["value"] or 0.0)

    async def _get_available_margin(self) -> float:
        """Свободная маржа (availableBalance) — потолок объёма, чтобы не ловить -2019."""
        now = time.time()
        cache = getattr(self, "_avail_margin_cache", None)
        if cache and cache.get("value") is not None and (now - float(cache.get("ts", 0.0))) < 30.0:
            return float(cache["value"])
        try:
            acct = await self.client.futures_account()
            avail = float(acct.get("availableBalance") or 0.0)
            self._avail_margin_cache = {"value": avail, "ts": now}
            return avail
        except Exception as e:
            self.log.debug(f"[CAP] available margin fetch failed: {e}")
        return 0.0

    async def _notional_cap_usd(self) -> float:
        """Потолок нотионала: min(абсолютный, % от equity, доступная маржа×плечо, биржевой брекет)."""
        caps = []
        try:
            abs_cap = float(getattr(self.cfg, "max_position_notional_usd", 0.0) or 0.0)
        except (TypeError, ValueError):
            abs_cap = 0.0
        if abs_cap > 0:
            caps.append(abs_cap)
        try:
            pct = float(getattr(self.cfg, "max_position_pct_equity", 0.0) or 0.0)
        except (TypeError, ValueError):
            pct = 0.0
        if pct > 0:
            eq = await self._get_equity()
            if eq > 0:
                caps.append(eq * pct / 100.0)
        # Доступная маржа × плечо (с запасом 5%) — чтобы хедж/вход не упирались в -2019.
        try:
            avail = await self._get_available_margin()
            if avail > 0:
                lev = int(self.cfg.leverage) if self.cfg.leverage else 1
                caps.append(avail * lev * 0.95)
        except Exception:
            pass
        bracket_cap = await self._get_symbol_notional_cap()
        if bracket_cap and bracket_cap > 0:
            caps.append(bracket_cap)
        return min(caps) if caps else 0.0

    def _log_leverage_clamp(self, configured: int, effective: int) -> None:
        """Логирует зажим плеча на старте и при каждом изменении, но не каждый вход."""
        if effective >= configured:
            return
        if self._last_leverage_log == (configured, effective):
            return
        self._last_leverage_log = (configured, effective)
        self.log.info(f"[LEVERAGE] clamped {configured} → {effective} (symbol max)")

    async def _set_leverage(self) -> None:
        """Применяет плечо, зажимая его по биржевому максимуму символа.

        Никогда не бросает наружу: любые ошибки (в т.ч. -4028) только
        логируются, чтобы не срывать открытие позиции вызывающим кодом.
        """
        symbol = getattr(self.cfg, "symbol", None)
        if not symbol or self.client is None:
            self.log.warning("[LEVERAGE] symbol/client unavailable — skipping leverage change")
            return
        try:
            configured = int(self.cfg.leverage)
        except (TypeError, ValueError):
            self.log.warning(
                f"[LEVERAGE] invalid configured leverage={self.cfg.leverage!r} — skipping"
            )
            return
        if configured < 1:
            self.log.warning(
                f"[LEVERAGE] invalid configured leverage={configured} (<1) — skipping"
            )
            return

        max_allowed: Optional[int] = None
        try:
            max_allowed = await self._get_symbol_max_leverage()
        except Exception as e:
            self.log.warning(f"[LEVERAGE] bracket read failed for {symbol}: {e}")

        effective = configured
        if max_allowed is not None and max_allowed >= 1:
            effective = min(configured, max_allowed)

        try:
            await self.client.futures_change_leverage(symbol=symbol, leverage=effective)
            self._log_leverage_clamp(configured, effective)
            return
        except BinanceAPIException as e:
            if getattr(e, "code", None) != -4028:
                self.log.warning(
                    f"[LEVERAGE] could not set {effective}x for {symbol}: {e}"
                )
                return
            # -4028: значение не помещается в bracket-лимиты. Один retry с
            # распарсенным максимумом, иначе с безопасным fallback 20x.
            fallback = max_allowed if (max_allowed is not None and max_allowed >= 1) else 20
            if fallback == effective:
                self.log.warning(
                    f"[LEVERAGE] -4028 for {effective}x on {symbol}; symbol max "
                    f"unknown or unchanged — retry skipped"
                )
                return
            self.log.warning(
                f"[LEVERAGE] -4028 for {effective}x on {symbol} — "
                f"falling back to {fallback}x"
            )
            try:
                await self.client.futures_change_leverage(symbol=symbol, leverage=fallback)
                self._log_leverage_clamp(configured, fallback)
            except Exception as e2:
                self.log.warning(
                    f"[LEVERAGE] fallback to {fallback}x failed for {symbol}: {e2}"
                )
        except Exception as e:
            self.log.warning(f"[LEVERAGE] could not set {effective}x for {symbol}: {e}")

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
