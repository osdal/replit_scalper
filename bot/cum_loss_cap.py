"""Cumulative loss cap для reverse-цепочки.

Суммарный результат цикла = реализованный net всех закрытых ног (с комиссиями)
+ текущий unrealized открытой позиции. Как только суммарный УБЫТОК достигает
``pct``% от депозита цикла — цикл должен быть закрыт немедленно (не дожидаясь
закрытия ноги).

Пример (депозит $40, 5% = $2.00):
    ноги: -0.1, -0.3, -1.0  → realized = -1.4
    4-я нога плавает на -0.6 → total = -2.0 → cap сработал.

Ключевые решения:
* Депозит фиксируется ОДИН раз на цикл (wallet на старте цикла), а не берётся
  заново: иначе по мере реализации убытков база (текущий баланс) сжимается и
  порог «уезжает» ($40→$38.6: лимит $1.93 вместо $2.00).
* Результат считается со знаком: прибыльная нога/плавающий плюс компенсируют
  убыток (как в формулировке «-0.1 -0.3 -1.0 ...»), а не обнуляются.
* Любой сбой получения данных НЕ трактуется как «убытка нет»: проверка
  пропускается и сбой логируется (не чаще раза в ``warn_interval_sec``).
* Модуль не импортирует ни binance, ни pandas — тестируется на заглушках.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

EPS = 1e-9


@dataclass
class CapSnapshot:
    realized_pnl: float      # net реализованный результат цикла (<0 — убыток)
    unrealized_pnl: float    # плавающий результат открытой позиции (<0 — убыток)
    ref: float               # депозит цикла, от которого считается порог
    pct: float               # лимит, % от депозита
    threshold: float         # ref * pct / 100 (в USD)

    @property
    def total_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl

    @property
    def cum_loss(self) -> float:
        """Суммарный убыток цикла в USD (>0 — убыток, <=0 — цикл в плюсе)."""
        return -self.total_pnl

    @property
    def hit(self) -> bool:
        return self.threshold > 0 and self.cum_loss + EPS >= self.threshold

    def describe(self) -> str:
        return (
            f"cum_loss={self.cum_loss:.4f} threshold={self.threshold:.4f} "
            f"({self.pct}% of {self.ref:.2f}) "
            f"realized={self.realized_pnl:+.4f} unrealized={self.unrealized_pnl:+.4f}"
        )


class CumLossCap:
    def __init__(
        self,
        pct: float,
        fixed_ref_usd: float = 0.0,
        interval_sec: float = 1.0,
        realized_ttl_sec: float = 30.0,
        warn_interval_sec: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.pct = float(pct or 0.0)
        self.fixed_ref_usd = float(fixed_ref_usd or 0.0)
        self.interval_sec = float(interval_sec)
        self.realized_ttl_sec = float(realized_ttl_sec)
        self.warn_interval_sec = float(warn_interval_sec)
        self._clock = clock
        self._ref: dict[str, float] = {}
        self._ts: dict[str, float] = {}
        self._realized: dict[str, tuple[int, float, float]] = {}  # step, ts, net
        self._warn_ts: dict[str, float] = {}

    @property
    def enabled(self) -> bool:
        return self.pct > 0

    def reset(self, symbol: str) -> None:
        """Сбросить состояние цикла (вызывать при флэте / новом цикле)."""
        self._ref.pop(symbol, None)
        self._ts.pop(symbol, None)
        self._realized.pop(symbol, None)
        self._warn_ts.pop(symbol, None)

    def _warn(self, symbol: str, log, msg: str) -> None:
        now = self._clock()
        if now - self._warn_ts.get(symbol, -1e18) < self.warn_interval_sec:
            return
        self._warn_ts[symbol] = now
        log.warning(f"[REVERSE] cum-loss cap check skipped | symbol={symbol} {msg}")

    async def evaluate(
        self,
        symbol: str,
        order_mgr,
        tracker,
        log,
        *,
        fresh: bool = False,
        net_realized: Optional[float] = None,
        unrealized: Optional[float] = None,
    ) -> Optional[CapSnapshot]:
        """Считает снимок цикла. None — данных недостаточно (сбой запроса).

        fresh        — не использовать кэш реализованного (после закрытия ноги).
        net_realized — уже известный net цикла (если caller его только что считал).
        unrealized   — уже известный unrealized книги (если caller его только что взял).
        """
        pos = tracker.position
        step = int(getattr(pos, "reverse_chain_step", 0) or 0) if pos is not None else 0
        now = self._clock()

        # --- реализованный net цикла ---------------------------------------
        if net_realized is None:
            cached = self._realized.get(symbol)
            if (not fresh and cached is not None and cached[0] == step
                    and now - cached[1] < self.realized_ttl_sec):
                net_realized = cached[2]
            else:
                try:
                    net_realized, _, _ = await tracker.cycle_realized_net()
                except Exception as e:  # noqa: BLE001
                    self._warn(symbol, log, f"realized unavailable: {e!r}")
                    return None
                if net_realized is None:
                    self._warn(symbol, log, "realized unavailable (exchange fills)")
                    return None
        net_realized = float(net_realized)
        self._realized[symbol] = (step, now, net_realized)

        # --- плавающий результат -------------------------------------------
        if unrealized is None:
            try:
                unrealized = await order_mgr.get_unrealized_pnl(fresh=True)
            except Exception as e:  # noqa: BLE001
                self._warn(symbol, log, f"unrealized unavailable: {e!r}")
                return None
            if unrealized is None:
                self._warn(symbol, log, "unrealized unavailable (position fetch failed)")
                return None
        unrealized = float(unrealized)

        # --- депозит цикла (фиксируем один раз) -------------------------------
        ref = self.fixed_ref_usd
        if ref <= 0:
            ref = self._ref.get(symbol, 0.0)
            if ref <= 0:
                try:
                    wallet = float(await order_mgr.get_balance("live", force=True))
                except Exception as e:  # noqa: BLE001
                    self._warn(symbol, log, f"balance unavailable: {e!r}")
                    return None
                if wallet <= 0:
                    self._warn(symbol, log, f"balance invalid: {wallet}")
                    return None
                # wallet сейчас = депозит на старте цикла + net реализованного
                ref = wallet - net_realized
                if ref <= 0:
                    ref = wallet
                self._ref[symbol] = ref

        return CapSnapshot(
            realized_pnl=net_realized,
            unrealized_pnl=unrealized,
            ref=ref,
            pct=self.pct,
            threshold=ref * self.pct / 100.0,
        )

    async def should_close(
        self, symbol: str, order_mgr, tracker, log
    ) -> Optional[CapSnapshot]:
        """Периодическая проверка (не чаще interval_sec).

        Возвращает снимок, если лимит достигнут и цикл надо закрыть, иначе None.
        """
        if not self.enabled:
            return None
        pos = tracker.position
        if (not tracker.has_open_position() or pos is None
                or not getattr(pos, "is_reverse", False)):
            self.reset(symbol)
            return None
        now = self._clock()
        if now - self._ts.get(symbol, -1e18) < self.interval_sec:
            return None
        self._ts[symbol] = now
        snap = await self.evaluate(symbol, order_mgr, tracker, log)
        if snap is not None and snap.hit:
            return snap
        return None
