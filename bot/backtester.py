import logging
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from config import Config
from market_data import get_historical_klines
from strategy import calculate_indicators, get_signal, Signal
from position_tracker import PositionTracker
from order_manager import calc_quantity


@dataclass
class TradeResult:
    direction: str
    entry_price: float
    exit_price: float
    qty: float
    pnl: float
    exit_reason: str
    entry_time: pd.Timestamp
    exit_time: pd.Timestamp


@dataclass
class BacktestStats:
    trades: List[TradeResult] = field(default_factory=list)
    initial_balance: float = 0.0
    final_balance: float = 0.0

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def losses(self) -> int:
        return sum(1 for t in self.trades if t.pnl <= 0)

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.wins / self.total_trades * 100

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def max_drawdown(self) -> float:
        if not self.trades:
            return 0.0
        balance = self.initial_balance
        peak = balance
        max_dd = 0.0
        for t in self.trades:
            balance += t.pnl
            if balance > peak:
                peak = balance
            dd = (peak - balance) / peak * 100
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @property
    def avg_win(self) -> float:
        wins = [t.pnl for t in self.trades if t.pnl > 0]
        return sum(wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = [t.pnl for t in self.trades if t.pnl <= 0]
        return sum(losses) / len(losses) if losses else 0.0


async def run_backtest(cfg: Config, client, logger: logging.Logger) -> BacktestStats:
    logger.info(
        f"[BACKTEST] Starting | {cfg.symbol} {cfg.timeframe} "
        f"{cfg.backtest_start} → {cfg.backtest_end}"
    )

    df = await get_historical_klines(
        client=client,
        symbol=cfg.symbol,
        interval=cfg.timeframe,
        start=cfg.backtest_start,
        end=cfg.backtest_end,
    )

    logger.info(f"[BACKTEST] Loaded {len(df)} candles")

    df = calculate_indicators(df, cfg)
    df.dropna(inplace=True)

    stats = BacktestStats(initial_balance=cfg.paper_balance)
    balance = cfg.paper_balance
    tracker = PositionTracker(cfg, logger)

    for i in range(1, len(df)):
        window = df.iloc[: i + 1]
        current_candle = df.iloc[i]
        current_price = float(current_candle["close"])
        current_time = df.index[i]

        if tracker.has_open_position():
            hit = tracker.check(current_price)
            if hit:
                pos = tracker.position
                pnl = tracker.apply_hit(hit, current_price)
                balance += pnl
                stats.trades.append(
                    TradeResult(
                        direction=pos.direction,
                        entry_price=pos.entry_price,
                        exit_price=current_price,
                        qty=pos.total_qty,
                        pnl=pnl,
                        exit_reason=hit,
                        entry_time=pos.entry_timestamp,
                        exit_time=current_time,
                    )
                )
                logger.info(
                    f"[BACKTEST] Trade closed | {hit} price={current_price:.4f} "
                    f"pnl={pnl:.4f} balance={balance:.2f}"
                )
            continue

        signal = get_signal(window, cfg)
        if signal is None:
            continue

        qty = calc_quantity(
            balance=balance,
            risk_pct=cfg.risk_pct,
            sl_pct=cfg.sl_pct,
            entry_price=signal.entry_price,
            leverage=cfg.leverage,
        )

        logger.info(
            f"[BACKTEST] Signal {signal.direction} | entry={signal.entry_price} "
            f"SL={signal.sl_price} TP1={signal.tp1_price} TP2={signal.tp2_price}"
        )
        tracker.open(signal, round(qty, 6))

    if tracker.has_open_position():
        pos = tracker.position
        last_price = float(df.iloc[-1]["close"])
        pnl = tracker.position.unrealized_pnl(last_price)
        logger.info(
            f"[BACKTEST] Open position at end | unrealized_pnl={pnl:.4f}"
        )

    stats.final_balance = balance
    _print_stats(stats, logger)
    return stats


def _print_stats(stats: BacktestStats, logger: logging.Logger) -> None:
    logger.info("=" * 60)
    logger.info("[BACKTEST] RESULTS")
    logger.info(f"  Total trades:    {stats.total_trades}")
    logger.info(f"  Wins / Losses:   {stats.wins} / {stats.losses}")
    logger.info(f"  Win rate:        {stats.win_rate:.1f}%")
    logger.info(f"  Total PnL:       {stats.total_pnl:.4f} USDT")
    logger.info(f"  Avg win:         {stats.avg_win:.4f} USDT")
    logger.info(f"  Avg loss:        {stats.avg_loss:.4f} USDT")
    logger.info(f"  Max drawdown:    {stats.max_drawdown:.2f}%")
    logger.info(f"  Initial balance: {stats.initial_balance:.2f} USDT")
    logger.info(f"  Final balance:   {stats.final_balance:.2f} USDT")
    logger.info(f"  Return:          {(stats.final_balance - stats.initial_balance) / stats.initial_balance * 100:.2f}%")
    logger.info("=" * 60)
