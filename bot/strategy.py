from dataclasses import dataclass
from typing import Optional

import pandas as pd

from config import Config


@dataclass
class Signal:
    direction: str  # "LONG" | "SHORT"
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    timestamp: pd.Timestamp


def calculate_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=cfg.ema_fast, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=cfg.ema_slow, adjust=False).mean()
    df["volume_ma"] = df["volume"].rolling(window=cfg.volume_ma_period).mean()
    return df


def calculate_htf_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """Pre-compute EMA on higher timeframe DataFrame."""
    df = df.copy()
    df["htf_ema_fast"] = df["close"].ewm(span=cfg.htf_ema_fast, adjust=False).mean()
    df["htf_ema_slow"] = df["close"].ewm(span=cfg.htf_ema_slow, adjust=False).mean()
    return df


def get_htf_trend(df_htf: pd.DataFrame, timestamp: pd.Timestamp) -> Optional[str]:
    """
    Returns 'LONG', 'SHORT', or None based on HTF EMA at the given timestamp.
    Finds the last HTF candle that closed at or before the working TF candle.
    """
    mask = df_htf.index <= timestamp
    if not mask.any():
        return None
    row = df_htf[mask].iloc[-1]
    if pd.isna(row.get("htf_ema_fast")) or pd.isna(row.get("htf_ema_slow")):
        return None
    return "LONG" if row["htf_ema_fast"] > row["htf_ema_slow"] else "SHORT"


def get_htf_trend_latest(df_htf: pd.DataFrame) -> Optional[str]:
    """Returns current HTF trend from the latest available candle."""
    if df_htf is None or len(df_htf) == 0:
        return None
    row = df_htf.iloc[-1]
    if "htf_ema_fast" not in df_htf.columns:
        return None
    if pd.isna(row["htf_ema_fast"]) or pd.isna(row["htf_ema_slow"]):
        return None
    return "LONG" if row["htf_ema_fast"] > row["htf_ema_slow"] else "SHORT"


def get_signal(
    df: pd.DataFrame,
    cfg: Config,
    htf_trend: Optional[str] = None,
) -> Optional[Signal]:
    if len(df) < cfg.ema_slow + 2:
        return None

    prev = df.iloc[-2]
    curr = df.iloc[-1]

    if pd.isna(prev["ema_fast"]) or pd.isna(prev["ema_slow"]):
        return None
    if pd.isna(curr["ema_fast"]) or pd.isna(curr["ema_slow"]):
        return None
    if pd.isna(curr["volume_ma"]):
        return None

    volume_ok = curr["volume"] >= curr["volume_ma"] * cfg.volume_multiplier

    prev_cross_above = prev["ema_fast"] <= prev["ema_slow"]
    curr_cross_above = curr["ema_fast"] > curr["ema_slow"]
    long_signal = prev_cross_above and curr_cross_above and volume_ok

    prev_cross_below = prev["ema_fast"] >= prev["ema_slow"]
    curr_cross_below = curr["ema_fast"] < curr["ema_slow"]
    short_signal = prev_cross_below and curr_cross_below and volume_ok

    if not long_signal and not short_signal:
        return None

    direction = "LONG" if long_signal else "SHORT"

    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        return None

    entry = curr["close"]
    sl_dist = entry * cfg.sl_pct / 100
    tp1_dist = entry * cfg.tp1_pct / 100
    tp2_dist = entry * cfg.tp2_pct / 100

    if direction == "LONG":
        sl_price = entry - sl_dist
        tp1_price = entry + tp1_dist
        tp2_price = entry + tp2_dist
    else:
        sl_price = entry + sl_dist
        tp1_price = entry - tp1_dist
        tp2_price = entry - tp2_dist

    return Signal(
        direction=direction,
        entry_price=entry,
        sl_price=round(sl_price, 4),
        tp1_price=round(tp1_price, 4),
        tp2_price=round(tp2_price, 4),
        timestamp=curr.name if isinstance(curr.name, pd.Timestamp) else pd.Timestamp(curr.name),
    )
