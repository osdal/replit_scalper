"""Читатель WS не должен блокироваться обработчиком свечи.

Регресс: при бане обработчик свечи спал до снятия бана прямо в цикле чтения
сокета. Очередь сокета переполнялась (BinanceWebsocketQueueOverflow), поток
данных умирал, бот переставал видеть свечи и цены до конца бана.

Теперь читатель только кладёт свечи в очередь, а обрабатывает их отдельная
задача. Проверяем по времени вызовов stream.recv(): второй кадр читается,
пока первый ещё обрабатывается.
"""
import asyncio
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import market_data as md  # noqa: E402


def _kline_msg(stream: str, close: float, ts_ms: int) -> dict:
    return {
        "stream": stream,
        "data": {
            "e": "kline",
            "k": {"x": True, "t": ts_ms, "o": "1", "h": "1", "l": "1",
                  "c": str(close), "v": "1"},
        },
    }


class _FakeStream:
    def __init__(self, msgs, recv_times):
        # Список общий с _FakeBM: после исчерпания он не «оживает» на реконнекте.
        self._msgs = msgs
        self._recv_times = recv_times

    async def recv(self):
        if self._msgs:
            self._recv_times.append(time.monotonic())
            return self._msgs.pop(0)
        raise asyncio.TimeoutError


class _FakeBM:
    def __init__(self, msgs, recv_times):
        self._msgs = msgs
        self._recv_times = recv_times

    def futures_multiplex_socket(self, streams, category=None):
        msgs, recv_times = self._msgs, self._recv_times

        class _CM:
            async def __aenter__(self):
                return _FakeStream(msgs, recv_times)

            async def __aexit__(self, *a):
                return False

        return _CM()


def test_slow_handler_does_not_block_ws_reader(monkeypatch):
    stream = "btcusdt@kline_5m"
    msgs = [
        _kline_msg(stream, 100.0, 1000),
        _kline_msg(stream, 200.0, 2000),
    ]
    recv_times = []
    monkeypatch.setattr(md, "BinanceSocketManager", lambda client: _FakeBM(msgs, recv_times))

    async def scenario():
        seen = []
        done = asyncio.Event()

        async def handler(candle):
            # Имитация «занятого» обработчика: REST/бан/сеть.
            await asyncio.sleep(0.5)
            seen.append(float(candle["close"]))
            if len(seen) >= 2:
                done.set()

        shutdown = asyncio.Event()
        task = asyncio.create_task(
            md.start_kline_websocket(
                client=None,
                symbol="BTCUSDT",
                handlers={"5m": handler},
                logger=None,
                shutdown_event=shutdown,
                ping_timeout=5,
            )
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=5.0)
        finally:
            shutdown.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return seen

    seen = asyncio.run(scenario())

    assert seen == [100.0, 200.0], "все свечи должны быть обработаны по порядку"
    assert len(recv_times) >= 2
    gap = recv_times[1] - recv_times[0]
    assert gap < 0.3, (
        f"второй кадр прочитан через {gap:.2f}s — читатель заблокирован обработчиком"
    )


def test_handler_exception_does_not_kill_consumer(monkeypatch):
    stream = "btcusdt@kline_5m"
    msgs = [
        _kline_msg(stream, 100.0, 1000),
        _kline_msg(stream, 200.0, 2000),
    ]
    recv_times = []
    monkeypatch.setattr(md, "BinanceSocketManager", lambda client: _FakeBM(msgs, recv_times))

    async def scenario():
        seen = []
        done = asyncio.Event()

        async def handler(candle):
            seen.append(float(candle["close"]))
            if len(seen) == 1:
                raise RuntimeError("boom")
            done.set()

        shutdown = asyncio.Event()
        task = asyncio.create_task(
            md.start_kline_websocket(
                client=None,
                symbol="BTCUSDT",
                handlers={"5m": handler},
                logger=None,
                shutdown_event=shutdown,
                ping_timeout=5,
            )
        )
        try:
            await asyncio.wait_for(done.wait(), timeout=5.0)
        finally:
            shutdown.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return seen

    seen = asyncio.run(scenario())
    assert seen == [100.0, 200.0]
