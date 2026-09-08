import logging
from dataclasses import dataclass, field
from typing import List, Optional

import pandas as pd

from config import Config
from market_data import get_historical_klines
from strategy import calculate_indicators, calculate_htf_indicators, get_signal, get_htf_trend, Signal
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
    preset: str = ""
    sl_price: float = 0.0
    tp1_price: float = 0.0
    tp2_price: float = 0.0
    commission: float = 0.0
    atr_value: float = 0.0          # ATR (абс.) на момент входа
    intrabar_return: float = 0.0    # (close-open)/open свечи входа: движение внутри свечи сигнала
    rsi_at_entry: float = 0.0       # RSI на свече входа
    regime_adx: float = 0.0        # ADX на момент входа (сила тренда)
    regime_atr_pct: float = 0.0    # ATR% на момент входа (волатильность)
    regime_trend: str = ""         # "LONG"|"SHORT"|"" — наклон EMA fast>slow на входе
    voting_bases: list = field(default_factory=list)  # стратегии, согласовавшие сигнал (consensus)


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

    df_htf: Optional[pd.DataFrame] = None
    if cfg.htf_enabled:
        df_htf = await get_historical_klines(
            client=client,
            symbol=cfg.symbol,
            interval=cfg.htf_timeframe,
            start=cfg.backtest_start,
            end=cfg.backtest_end,
        )
        df_htf = calculate_htf_indicators(df_htf, cfg)
        logger.info(f"[BACKTEST] Loaded {len(df_htf)} HTF candles ({cfg.htf_timeframe})")

    stats = run_backtest_on_df(df, cfg, logger, df_htf=df_htf)
    _print_stats(stats, logger)
    return stats


def run_backtest_on_df(
    df: pd.DataFrame,
    cfg: Config,
    logger: logging.Logger,
    df_htf: Optional[pd.DataFrame] = None,
    enabled_presets: Optional[list[str]] = None,
    precomputed: bool = False,
) -> BacktestStats:
    """
    Runs backtest on a pre-downloaded DataFrame.
    Optionally accepts a pre-computed HTF DataFrame for trend filtering,
    a list of enabled presets to restrict the signals to,
    and `precomputed=True` to skip indicator recalculation (df already has
    indicator columns and NaN rows removed).
    """
    if not precomputed:
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
                pnl_result = tracker.apply_hit(hit, current_price)
                pnl = pnl_result[0] if isinstance(pnl_result, tuple) else pnl_result
                # Комиссия как в live: commission_pct taker на вход и выход
                fee = getattr(cfg, "commission_pct", 0.0) / 100.0
                commission = (abs(pos.entry_price) + abs(current_price)) * abs(pos.total_qty) * fee
                net_pnl = pnl - commission
                balance += net_pnl
                # Позиция полностью закрыта? Если нет (partial TP1 при tp1_close_pct<100) —
                # не пишем финальный трейд, дождёмся закрытия остатка.
                if pos.closed or tracker.position is None:
                    stats.trades.append(
                        TradeResult(
                            direction=pos.direction,
                            entry_price=pos.entry_price,
                            exit_price=current_price,
                            qty=pos.total_qty,
                            pnl=net_pnl,
                            exit_reason=hit,
                            entry_time=pos.entry_timestamp,
                            exit_time=current_time,
                            preset=pos.preset,
                            sl_price=pos.sl_price,
                            tp1_price=pos.tp1_price,
                            tp2_price=pos.tp2_price,
                            commission=round(commission, 6),
                            atr_value=pos.entry_atr,
                            intrabar_return=pos.intrabar_return,
                            rsi_at_entry=pos.entry_rsi,
                            regime_adx=pos.regime_adx,
                            regime_atr_pct=pos.regime_atr_pct,
                            regime_trend=pos.regime_trend,
                            voting_bases=list(pos.voting_bases or []),
                        )
                    )
            continue

        htf_trend: Optional[str] = None
        if cfg.htf_enabled and df_htf is not None:
            htf_trend = get_htf_trend(df_htf, current_time)

        # ADX текущей свечи (для режимного consensus и ADX-фильтра)
        try:
            candle_adx = float(current_candle.get("adx", 0) or 0) if "adx" in df.columns else 0.0
        except Exception:
            candle_adx = 0.0

        # Фильтр режима рынка по ADX на свече входа (если задан min/max в cfg)
        min_adx = getattr(cfg, "min_adx_filter", 0.0)
        max_adx = getattr(cfg, "max_adx_filter", 0.0)
        if (min_adx > 0 and candle_adx < min_adx) or (max_adx > 0 and candle_adx > max_adx):
            continue

        # Режимно-адаптивный consensus: зависит от зоны ADX
        consensus = getattr(cfg, "min_consensus", 1)
        if candle_adx < 15:
            consensus = getattr(cfg, "consensus_flat", None) or consensus
        elif candle_adx < 25:
            consensus = getattr(cfg, "consensus_weak", None) or consensus
        else:
            consensus = getattr(cfg, "consensus_trend", None) or consensus

        signal = get_signal(window, cfg, htf_trend=htf_trend,
                            enabled_presets=enabled_presets,
                            min_consensus=consensus)
        if signal is None:
            continue

        # В тренде (ADX>=25) блокируем SHORT, если включено
        if candle_adx >= 25 and getattr(cfg, "trend_block_short", False) and signal.direction == "SHORT":
            continue

        # Блокировка по часу UTC входа
        block_hours = getattr(cfg, "block_hours_utc", None) or []
        if block_hours:
            try:
                hour_utc = int(current_time.hour)
                if hour_utc in [int(h) for h in block_hours]:
                    continue
            except Exception:
                pass

        # Исключённые пресеты
        excluded = set(getattr(cfg, "excluded_presets", None) or [])
        if excluded and signal.preset in excluded:
            continue

        # Sizing как в live: приоритет у fixed_risk_usd / margin_pct / fixed_notional /
        # fixed_qty, иначе risk_pct от баланса. ВАЖНО: qty считается от РЕАЛЬНОГО
        # расстояния до SL сигнала (signal.sl_price), а не от cfg.sl_pct — иначе при
        # ATR-стопах (SL = 1.5×ATR) риск на сделку не совпадает с заявленным.
        entry = signal.entry_price
        actual_sl_pct = abs(entry - signal.sl_price) / entry * 100 if entry and signal.sl_price else cfg.sl_pct
        if cfg.margin_pct and cfg.margin_pct > 0:
            margin = round(balance * cfg.margin_pct / 100, 1)
            qty = (margin * cfg.leverage) / entry
        elif cfg.fixed_notional_usd and cfg.fixed_notional_usd > 0:
            qty = (cfg.fixed_notional_usd * cfg.leverage) / entry
        elif cfg.fixed_qty and cfg.fixed_qty > 0:
            qty = cfg.fixed_qty
        elif cfg.fixed_risk_usd and cfg.fixed_risk_usd > 0:
            # Фиксированный риск в USD: qty = risk_usd / (entry * actual_sl_pct%)
            qty = cfg.fixed_risk_usd / (entry * actual_sl_pct / 100)
        else:
            qty = calc_quantity(
                balance=balance,
                risk_pct=cfg.risk_pct,
                sl_pct=actual_sl_pct,
                entry_price=entry,
                leverage=cfg.leverage,
            )
        qty = round(qty, 6)
        # Sanity: не входить, если маржа (notional/leverage) превышает баланс —
        # как в live (OrderManager проверяет margin > balance).
        notional = qty * entry
        leverage = getattr(cfg, "leverage", 1) or 1
        if leverage > 0 and notional / leverage > balance * 1.0:
            continue
        tracker.open(signal, qty)

        # Режим рынка на момент входа (для анализа зависимости пресетов от состояния рынка)
        if tracker.position is not None:
            last_row = df.iloc[i]
            try:
                adx_val = float(last_row.get("adx", 0) or 0) if "adx" in df.columns else 0.0
            except Exception:
                adx_val = 0.0
            try:
                entry_price = float(signal.entry_price or 0)
                atr_abs = float(last_row.get("atr", 0) or 0)
                atr_pct = atr_abs / entry_price * 100 if entry_price and atr_abs else 0.0
            except Exception:
                atr_pct = 0.0
            try:
                ef = float(signal.ema_fast or 0)
                es = float(signal.ema_slow or 0)
                regime_trend = "LONG" if ef > es else ("SHORT" if es > ef else "")
            except Exception:
                regime_trend = ""
            tracker.position.regime_adx = round(adx_val, 1)
            tracker.position.regime_atr_pct = round(atr_pct, 3)
            tracker.position.regime_trend = regime_trend
            # Внутрисвечевое движение свечи сигнала: (close-open)/open
            try:
                o = float(current_candle.get("open", 0) or 0)
                c = float(current_candle.get("close", 0) or 0)
                tracker.position.intrabar_return = round((c - o) / o * 100, 3) if o else 0.0
            except Exception:
                tracker.position.intrabar_return = 0.0

    stats.final_balance = balance
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
