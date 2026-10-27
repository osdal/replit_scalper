"""Fallback при -2022 «ReduceOnly Order is rejected» (bot/order_manager.py).

27.10 на testnet у AVAXUSDT позиция SHORT 4 жила, открытых ордеров не было, но
биржа показывала bidNotional=77.2 и отклоняла ЛЮБОЙ reduceOnly: и TP-лимит, и
закрытие рынком. Бот оставался без TP, а cum-loss cap / закрытие протухшей
цепочки физически не могли закрыть позицию. Теперь:
  * TP падает в условный TAKE_PROFIT_MARKET closePosition;
  * закрытие рынком падает в обычный MARKET ровно на объём позиции (с защитой
    от переворота, если позицию успели закрыть между чтением и отправкой).

Запуск: cd bot && python -m pytest tests/test_reduce_only_fallback.py -q
"""
import asyncio
import logging
import os
import sys
from types import SimpleNamespace

import pytest
from binance.exceptions import BinanceAPIException

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from order_manager import OrderManager  # noqa: E402

LOG = logging.getLogger("test-ro-fallback")
RO_MSG = "ReduceOnly Order is rejected."


def api_error(code: int, msg: str = RO_MSG) -> BinanceAPIException:
    return BinanceAPIException(
        SimpleNamespace(text="", request=None), 400,
        '{"code": %d, "msg": "%s"}' % (code, msg),
    )


def run(coro):
    return asyncio.run(coro)


class FakeClient:
    """reduceOnly всегда отклоняется; обычные ордера проходят."""

    def __init__(self, position_amt=-4.0, after_plain_amt=0.0, tp_error=None):
        self.position_amt = position_amt
        self.after_plain_amt = after_plain_amt
        self.tp_error = tp_error
        self.calls = []

    async def futures_create_order(self, **kw):
        self.calls.append(kw)
        if kw.get("reduceOnly"):
            raise api_error(-2022)
        if kw.get("type") == "TAKE_PROFIT_MARKET" and self.tp_error:
            raise api_error(self.tp_error)
        # обычный MARKET меняет позицию
        self.position_amt = self.after_plain_amt
        return {"orderId": 1, "status": "NEW"}

    async def futures_position_information(self, **kw):
        return [{"symbol": "AVAXUSDT", "positionAmt": str(self.position_amt),
                 "entryPrice": "8.759", "markPrice": "8.75"}]

    async def futures_cancel_all_open_orders(self, **kw):
        return None

    async def futures_get_open_algo_orders(self, **kw):
        return []

    async def futures_cancel_algo_order(self, **kw):
        return None


def make_om(client, symbol="AVAXUSDT"):
    cfg = SimpleNamespace(symbol=symbol, mode="live", taker_fee_pct=0.05,
                          reverse_profit_pct=0.1, tp1_close_pct=100)
    om = OrderManager(cfg, LOG, client=client)
    om._step_size = 1.0
    om._tick_size = 0.001
    om._price_precision = 3
    om._min_notional = 5.0
    return om


# --------------------------------------------------------------------------- #
#  TP
# --------------------------------------------------------------------------- #

def test_tp_falls_back_to_conditional_close_position():
    client = FakeClient()
    om = make_om(client)
    run(om._place_tp_limit("SHORT", 8.716, 4.0))

    assert len(client.calls) == 2
    first, second = client.calls
    assert first["type"] == "LIMIT" and first["reduceOnly"] is True
    assert first["side"] == "BUY"
    assert second["type"] == "TAKE_PROFIT_MARKET"
    assert second["side"] == "BUY"
    assert second["closePosition"] == "true"
    assert second["workingType"] == "MARK_PRICE"
    assert float(second["stopPrice"]) == pytest.approx(8.716)
    assert om.exchange_tp_conditional is True
    assert om.exchange_tp_price == pytest.approx(8.716)


def test_tp_limit_keeps_conditional_flag_false_on_success():
    class OkClient(FakeClient):
        async def futures_create_order(self, **kw):
            self.calls.append(kw)
            return {"orderId": 7}

    client = OkClient()
    om = make_om(client)
    om.exchange_tp_conditional = True  # был fallback — должен сброситься
    run(om._place_tp_limit("LONG", 7.885, 4.0))
    assert len(client.calls) == 1
    assert om.exchange_tp_conditional is False
    assert om.exchange_tp_price == pytest.approx(7.885)


def test_tp_other_api_errors_still_raise():
    class BusyClient(FakeClient):
        async def futures_create_order(self, **kw):
            raise api_error(-1013, "Filter failure: PRICE_FILTER")

    with pytest.raises(BinanceAPIException):
        run(make_om(BusyClient())._place_tp_limit("SHORT", 8.716, 4.0))


def test_tp_conditional_immediate_trigger_is_not_fatal():
    client = FakeClient(tp_error=-2021)
    om = make_om(client)
    run(om._place_tp_limit("SHORT", 8.716, 4.0))
    assert len(client.calls) == 2
    assert client.calls[1]["type"] == "TAKE_PROFIT_MARKET"
    assert om.exchange_tp_conditional is False


def test_tp_fallback_skips_partial_tp():
    """closePosition закрыл бы всю позицию — для частичного TP fallback запрещён."""
    client = FakeClient(position_amt=177.0)  # LONG 177, а TP на часть
    om = make_om(client)
    run(om._place_tp_limit("LONG", 0.1813, 88.0))
    assert len(client.calls) == 1
    assert client.calls[0]["type"] == "LIMIT"
    assert om.exchange_tp_conditional is False


def test_tp_fallback_skips_when_position_not_visible():
    client = FakeClient(position_amt=0.0)
    om = make_om(client)
    run(om._place_tp_limit("SHORT", 8.716, 4.0))
    assert len(client.calls) == 1
    assert om.exchange_tp_conditional is False


# --------------------------------------------------------------------------- #
#  Закрытие рынком
# --------------------------------------------------------------------------- #

def test_close_market_falls_back_to_plain_market():
    client = FakeClient(position_amt=-4.0, after_plain_amt=0.0)
    om = make_om(client)
    assert run(om.close_position_market("SHORT")) is True

    markets = [c for c in client.calls if c["type"] == "MARKET"]
    assert len(markets) == 2
    assert markets[0]["reduceOnly"] is True and markets[0]["side"] == "BUY"
    assert "reduceOnly" not in markets[1] and markets[1]["side"] == "BUY"
    assert float(markets[1]["quantity"]) == pytest.approx(4.0)


def test_close_market_repairs_flip():
    """Обычный MARKET без reduceOnly перевернул позицию — закрываем встречную."""
    client = FakeClient(position_amt=-4.0, after_plain_amt=4.0)
    om = make_om(client)
    assert run(om.close_position_market("SHORT")) is True

    markets = [c for c in client.calls if c["type"] == "MARKET"]
    assert len(markets) == 3
    assert markets[2]["side"] == "SELL"
    assert float(markets[2]["quantity"]) == pytest.approx(4.0)


def test_close_market_no_position_is_noop():
    client = FakeClient(position_amt=0.0)
    om = make_om(client)
    assert run(om.close_position_market("SHORT")) is False
    assert client.calls == []


def test_close_market_other_errors_still_fail():
    class BusyClient(FakeClient):
        async def futures_create_order(self, **kw):
            raise api_error(-1003, "Too many requests")

    om = make_om(BusyClient())
    assert run(om.close_position_market("SHORT")) is False


# --------------------------------------------------------------------------- #
#  SYNC / пересчёт TP: условный TP живёт в algo-книге
# --------------------------------------------------------------------------- #

class AlgoClient(FakeClient):
    def __init__(self, algo_orders, position_amt=4.0):
        super().__init__(position_amt=position_amt)
        self.algo_orders = algo_orders
        self.cancelled_algo = []
        self.cancelled_orders = []

    async def futures_get_open_orders(self, **kw):
        return []

    async def futures_get_open_algo_orders(self, **kw):
        return list(self.algo_orders)

    async def futures_cancel_algo_order(self, **kw):
        self.cancelled_algo.append(kw.get("algoId"))
        return {}

    async def futures_cancel_order(self, **kw):
        self.cancelled_orders.append(kw.get("orderId"))
        return {}


def _pos(**over):
    base = dict(direction="LONG", entry_price=6.948, tp1_price=7.885,
                tp2_price=7.885, sl_price=6.649, remaining_qty=4.0,
                total_qty=4.0, tp1_hit=False, is_reverse=True, mode="live",
                exchange_tp_price=7.885, backstop_algo_id=1000000258240181)
    base.update(over)
    return SimpleNamespace(**base)


def test_sync_recognizes_conditional_tp(monkeypatch):
    import main as bot_main

    client = AlgoClient([
        {"algoId": 555, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL",
         "closePosition": True, "triggerPrice": "7.885"},
        {"algoId": 1000000258240181, "clientAlgoId": "botsl_UNIUSDT_long",
         "orderType": "STOP_MARKET", "side": "SELL", "closePosition": True,
         "triggerPrice": "6.516"},
    ])
    om = make_om(client, symbol="UNIUSDT")
    placed = []

    async def fake_place(direction, price, qty):
        placed.append((direction, price, qty))

    monkeypatch.setattr(om, "_place_tp_limit", fake_place)
    run(bot_main._ensure_exchange_protection(om, om.cfg, _pos(), LOG))
    assert placed == []          # дубликат TP не выставлен
    assert om.backstop_algo_id == 1000000258240181


def test_recalc_cancels_conditional_tp_before_replacing():
    import main as bot_main

    client = AlgoClient([
        {"algoId": 555, "orderType": "TAKE_PROFIT_MARKET", "side": "SELL",
         "closePosition": True, "triggerPrice": "7.885"},
    ])
    om = make_om(client, symbol="UNIUSDT")
    pos = _pos()

    class Tracker:
        _trade_id = 1

        async def cycle_realized_net(self):
            return -3.7114, None, None

        def _save_state(self):
            pass

    changed = run(bot_main._recalc_reverse_cycle_tp(om, Tracker(), pos, LOG))
    assert changed is True
    assert 555 in client.cancelled_algo
    new_tps = [c for c in client.calls if c.get("reduceOnly")]
    assert len(new_tps) == 1
    assert pos.tp1_price == pytest.approx(om.exchange_tp_price)

