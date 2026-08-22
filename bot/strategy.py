from dataclasses import dataclass
from typing import Optional
import logging
import os
import time

import pandas as pd

from config import Config

# TEMP DEBUG: aggregate signal-filter diagnostics, flush a compact summary
# periodically so logs/sigdebug.log stays small (no per-candle spam).
logger = logging.getLogger("strategy")
if not logger.handlers:
    _tmp_fh = logging.FileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "sigdebug.log"), encoding="utf-8")
    _tmp_fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    logger.addHandler(_tmp_fh)
    logger.setLevel(logging.INFO)
    logger.propagate = False

_SIG_FLUSH_SEC = 1800  # flush aggregated counters every 30 min
_sig_last = time.monotonic()
_sig = {p: 0 for p in [
    "ema_cross", "sma_cross", "rsi_bounce", "rsi_divergence",
    "macd_momentum", "macd_hist_trend",
    "bb_bounce", "bb_squeeze_breakout",
    "stoch_bounce", "stoch_obos",
    "vwap_return", "vwap_band",
    "atr_breakout", "atr_trailing",
    "volume_spike", "volume_trend",
    "supertrend", "supertrend_reentry",
    "adx_trend", "ichimoku", "ichimoku_tk",
    "morning_star", "evening_star", "engulfing",
    "htf", "pass",
]}


def _sig_bump(key: str) -> None:
    global _sig_last
    _sig[key] = _sig.get(key, 0) + 1
    now = time.monotonic()
    if now - _sig_last >= _SIG_FLUSH_SEC:
        _sig_last = now
        parts = [f"{k}={v}" for k, v in _sig.items() if v > 0]
        logger.info(f"[SIGDEBUG] 30m summary: {' '.join(parts)}")
        for k in _sig:
            _sig[k] = 0


@dataclass
class Signal:
    direction: str  # "LONG" | "SHORT"
    entry_price: float
    sl_price: float
    tp1_price: float
    tp2_price: float
    timestamp: pd.Timestamp
    preset: str = "ema_cross"
    # индикаторы на момент сигнала
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    volume: float = 0.0
    volume_ma: float = 0.0
    rsi: float = 0.0
    macd: float = 0.0
    macd_signal: float = 0.0
    macd_hist: float = 0.0
    bb_upper: float = 0.0
    bb_middle: float = 0.0
    bb_lower: float = 0.0
    atr: float = 0.0
    mode: Optional[str] = None  # "paper"|"live"|None (None = наследует режим бота)


# ── Indicator calculations ────────────────────────────────────────────────

def calculate_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    df = df.copy()
    df["ema_fast"] = df["close"].ewm(span=cfg.ema_fast, adjust=False).mean()
    df["ema_slow"] = df["close"].ewm(span=cfg.ema_slow, adjust=False).mean()
    df["sma_fast"] = df["close"].rolling(window=cfg.ema_fast).mean()
    df["sma_slow"] = df["close"].rolling(window=cfg.ema_slow).mean()
    df["volume_ma"] = df["volume"].rolling(window=cfg.volume_ma_period).mean()

    # ATR
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift(1)).abs()
    tr3 = (df["low"] - df["close"].shift(1)).abs()
    df["tr"] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    df["atr"] = df["tr"].ewm(alpha=1 / max(cfg.adx_period, 1), adjust=False).mean()

    # ADX / DI
    if cfg.adx_threshold > 0:
        up_move = df["high"] - df["high"].shift(1)
        down_move = df["low"].shift(1) - df["low"]
        df["+dm"] = up_move.where((up_move > down_move) & (up_move > 0), 0.0)
        df["-dm"] = down_move.where((down_move > up_move) & (down_move > 0), 0.0)
        df["+dm_s"] = df["+dm"].ewm(alpha=1 / cfg.adx_period, adjust=False).mean()
        df["-dm_s"] = df["-dm"].ewm(alpha=1 / cfg.adx_period, adjust=False).mean()
        df["+di"] = 100 * df["+dm_s"] / df["atr"].replace(0, float("nan"))
        df["-di"] = 100 * df["-dm_s"] / df["atr"].replace(0, float("nan"))
        df["dx"] = 100 * (df["+di"] - df["-di"]).abs() / (df["+di"] + df["-di"])
        df["adx"] = df["dx"].ewm(alpha=1 / cfg.adx_period, adjust=False).mean()

    # RSI
    if cfg.rsi_period > 0:
        delta = df["close"].diff()
        gain = delta.where(delta > 0, 0.0)
        loss = (-delta).where(delta < 0, 0.0)
        avg_gain = gain.ewm(alpha=1 / cfg.rsi_period, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / cfg.rsi_period, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, float("nan"))
        df["rsi"] = 100 - (100 / (1 + rs))

    # MACD
    if cfg.macd_fast > 0 and cfg.macd_slow > 0:
        ema_fast = df["close"].ewm(span=cfg.macd_fast, adjust=False).mean()
        ema_slow = df["close"].ewm(span=cfg.macd_slow, adjust=False).mean()
        df["macd"] = ema_fast - ema_slow
        df["macd_signal"] = df["macd"].ewm(span=cfg.macd_signal, adjust=False).mean()
        df["macd_hist"] = df["macd"] - df["macd_signal"]

    # Bollinger Bands
    if cfg.bb_period > 0:
        df["bb_middle"] = df["close"].rolling(window=cfg.bb_period).mean()
        bb_std = df["close"].rolling(window=cfg.bb_period).std()
        df["bb_upper"] = df["bb_middle"] + cfg.bb_std * bb_std
        df["bb_lower"] = df["bb_middle"] - cfg.bb_std * bb_std

    # Stochastic Oscillator (14, 3, 3)
    if len(df) >= 14:
        low_min = df["low"].rolling(window=14).min()
        high_max = df["high"].rolling(window=14).max()
        stoch_range = (high_max - low_min).replace(0, float("nan"))
        df["stoch_k"] = 100 * (df["close"] - low_min) / stoch_range
        df["stoch_d"] = df["stoch_k"].rolling(window=3).mean()

    # VWAP
    df["vwap"] = _calc_vwap(df)

    # Supertrend (ATR-based, period=10, multiplier=3)
    df["supertrend"] = _calc_supertrend(df, period=10, multiplier=3.0)

    # Ichimoku Cloud
    ichimoku = _calc_ichimoku(df)
    df["ichimoku_tenkan"] = ichimoku["tenkan"]
    df["ichimoku_kijun"] = ichimoku["kijun"]
    df["ichimoku_senkou_a"] = ichimoku["senkou_a"]
    df["ichimoku_senkou_b"] = ichimoku["senkou_b"]
    df["ichimoku_chikou"] = ichimoku["chikou"]

    return df


def _calc_vwap(df: pd.DataFrame) -> pd.Series:
    typical = (df["high"] + df["low"] + df["close"]) / 3
    vwap_sum = (typical * df["volume"]).cumsum()
    vol_sum = df["volume"].cumsum()
    return vwap_sum / vol_sum.replace(0, float("nan"))


def _calc_supertrend(df: pd.DataFrame, period: int = 10, multiplier: float = 3.0) -> pd.Series:
    if len(df) < period + 1:
        return pd.Series([float("nan")] * len(df), index=df.index)
    hl2 = (df["high"] + df["low"]) / 2
    atr = df["atr"] if "atr" in df.columns else pd.Series([float("nan")] * len(df), index=df.index)
    basic_upper = hl2 + multiplier * atr
    basic_lower = hl2 - multiplier * atr
    supertrend = pd.Series([float("nan")] * len(df), index=df.index)
    direction = pd.Series([1] * len(df), index=df.index)
    for i in range(1, len(df)):
        if i < period:
            continue
        curr_close = df["close"].iloc[i]
        prev_st = supertrend.iloc[i - 1]
        if pd.isna(prev_st) or curr_close > prev_st:
            direction.iloc[i] = 1
        else:
            direction.iloc[i] = -1
        if direction.iloc[i] == 1:
            supertrend.iloc[i] = basic_lower.iloc[i] if pd.isna(prev_st) or basic_lower.iloc[i] > prev_st else prev_st
        else:
            supertrend.iloc[i] = basic_upper.iloc[i] if pd.isna(prev_st) or basic_upper.iloc[i] < prev_st else prev_st
    return supertrend


def _calc_ichimoku(df: pd.DataFrame) -> dict:
    high = df["high"]
    low = df["low"]
    close = df["close"]
    tenkan = (high.rolling(window=9).max() + low.rolling(window=9).min()) / 2
    kijun = (high.rolling(window=26).max() + low.rolling(window=26).min()) / 2
    senkou_a = (tenkan + kijun) / 2
    senkou_b = (high.rolling(window=52).max() + low.rolling(window=52).min()) / 2
    chikou = close.shift(-26)
    return {"tenkan": tenkan, "kijun": kijun, "senkou_a": senkou_a, "senkou_b": senkou_b, "chikou": chikou}


def calculate_htf_indicators(df: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    df = df.copy()
    df["htf_ema_fast"] = df["close"].ewm(span=cfg.htf_ema_fast, adjust=False).mean()
    df["htf_ema_slow"] = df["close"].ewm(span=cfg.htf_ema_slow, adjust=False).mean()
    return df


def get_htf_trend(df_htf: pd.DataFrame, timestamp: pd.Timestamp) -> Optional[str]:
    mask = df_htf.index <= timestamp
    if not mask.any():
        return None
    row = df_htf[mask].iloc[-1]
    if pd.isna(row.get("htf_ema_fast")) or pd.isna(row.get("htf_ema_slow")):
        return None
    return "LONG" if row["htf_ema_fast"] > row["htf_ema_slow"] else "SHORT"


def get_htf_trend_latest(df_htf: pd.DataFrame) -> Optional[str]:
    if df_htf is None or len(df_htf) == 0:
        return None
    row = df_htf.iloc[-1]
    if "htf_ema_fast" not in df_htf.columns:
        return None
    if pd.isna(row["htf_ema_fast"]) or pd.isna(row["htf_ema_slow"]):
        return None
    return "LONG" if row["htf_ema_fast"] > row["htf_ema_slow"] else "SHORT"


# ── Per-preset signal functions (fully independent) ────────────────────────

def _calc_atr_sl_tp(entry: float, atr_abs: float, base_sl_pct: float, base_tp_pct: float) -> tuple[float, float]:
    """Динамический SL по ATR: расширяем только если ATR высокий, cap 0.65%, TP = 2*SL."""
    atr_pct = (atr_abs / entry) * 100 if entry > 0 else 0.0
    if atr_pct > base_sl_pct / 1.5:
        dynamic_sl = min(1.5 * atr_pct, 0.65)
    else:
        dynamic_sl = base_sl_pct
    dynamic_tp = 2.0 * dynamic_sl
    return dynamic_sl, dynamic_tp


def _make_signal(df: pd.DataFrame, cfg: Config, direction: str, preset: str,
                 ema_fast: float = 0.0, ema_slow: float = 0.0,
                 volume: float = 0.0, volume_ma: float = 0.0) -> Optional[Signal]:
    from preset_config import get_preset_config
    curr = df.iloc[-1]
    entry = float(curr["close"])
    preset_cfg = get_preset_config(preset)
    sl_pct = preset_cfg.get("sl", cfg.sl_pct)
    tp_pct = preset_cfg.get("tp", cfg.tp1_pct)
    atr_abs = float(curr.get("atr", 0) or 0)
    dynamic_sl, dynamic_tp = _calc_atr_sl_tp(entry, atr_abs, sl_pct, tp_pct)
    sl_dist = entry * dynamic_sl / 100
    tp_dist = entry * dynamic_tp / 100
    if direction == "LONG":
        sl_price = entry - sl_dist
        tp1_price = entry + tp_dist
        tp2_price = entry + tp_dist
    else:
        sl_price = entry + sl_dist
        tp1_price = entry - tp_dist
        tp2_price = entry - tp_dist
    return Signal(
        direction=direction,
        entry_price=entry,
        sl_price=round(sl_price, 8),
        tp1_price=round(tp1_price, 8),
        tp2_price=round(tp2_price, 8),
        timestamp=curr.name if isinstance(curr.name, pd.Timestamp) else pd.Timestamp(curr.name),
        preset=preset,
        ema_fast=round(float(ema_fast), 4),
        ema_slow=round(float(ema_slow), 4),
        volume=round(float(volume), 2),
        volume_ma=round(float(volume_ma), 2),
        rsi=round(float(curr.get("rsi", 0) or 0), 2),
        macd=round(float(curr.get("macd", 0) or 0), 6),
        macd_signal=round(float(curr.get("macd_signal", 0) or 0), 6),
        macd_hist=round(float(curr.get("macd_hist", 0) or 0), 6),
        bb_upper=round(float(curr.get("bb_upper", 0) or 0), 4),
        bb_middle=round(float(curr.get("bb_middle", 0) or 0), 4),
        bb_lower=round(float(curr.get("bb_lower", 0) or 0), 4),
        atr=round(atr_abs, 6),
        mode=preset_cfg.get("mode") or cfg.mode,
    )


def _safe_series(df: pd.DataFrame, col: str) -> bool:
    return col in df.columns and not df[col].isna().all()


# 1. EMA cross
def get_signal_ema_cross(df: pd.DataFrame, cfg: Config,
                         htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < cfg.ema_slow + 2:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if not _safe_series(df, "ema_fast") or not _safe_series(df, "ema_slow") or not _safe_series(df, "volume_ma"):
        return None
    if pd.isna(prev["ema_fast"]) or pd.isna(prev["ema_slow"]) or pd.isna(curr["ema_fast"]) or pd.isna(curr["ema_slow"]) or pd.isna(curr["volume_ma"]):
        return None
    volume_ok = curr["volume"] >= curr["volume_ma"] * cfg.volume_multiplier
    prev_cross_above = prev["ema_fast"] <= prev["ema_slow"]
    curr_cross_above = curr["ema_fast"] > curr["ema_slow"]
    long_signal = prev_cross_above and curr_cross_above and volume_ok
    prev_cross_below = prev["ema_fast"] >= prev["ema_slow"]
    curr_cross_below = curr["ema_fast"] < curr["ema_slow"]
    short_signal = prev_cross_below and curr_cross_below and volume_ok
    cross_long = prev_cross_above and curr_cross_above
    cross_short = prev_cross_below and curr_cross_below
    if cross_long or cross_short:
        _sig_bump("ema_cross")
        if not long_signal and not short_signal:
            _sig_bump("vol")
    if not long_signal and not short_signal:
        return None
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    return _make_signal(df, cfg, direction, "ema_cross", curr["ema_fast"], curr["ema_slow"], curr["volume"], curr["volume_ma"])


# 2. SMA cross
def get_signal_sma_cross(df: pd.DataFrame, cfg: Config,
                         htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < cfg.ema_slow + 2:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if not _safe_series(df, "sma_fast") or not _safe_series(df, "sma_slow") or not _safe_series(df, "volume_ma"):
        return None
    if pd.isna(prev["sma_fast"]) or pd.isna(prev["sma_slow"]) or pd.isna(curr["sma_fast"]) or pd.isna(curr["sma_slow"]) or pd.isna(curr["volume_ma"]):
        return None
    volume_ok = curr["volume"] >= curr["volume_ma"] * cfg.volume_multiplier
    prev_cross_above = prev["sma_fast"] <= prev["sma_slow"]
    curr_cross_above = curr["sma_fast"] > curr["sma_slow"]
    long_signal = prev_cross_above and curr_cross_above and volume_ok
    prev_cross_below = prev["sma_fast"] >= prev["sma_slow"]
    curr_cross_below = curr["sma_fast"] < curr["sma_slow"]
    short_signal = prev_cross_below and curr_cross_below and volume_ok
    cross_long = prev_cross_above and curr_cross_above
    cross_short = prev_cross_below and curr_cross_below
    if cross_long or cross_short:
        _sig_bump("sma_cross")
        if not long_signal and not short_signal:
            _sig_bump("vol")
    if not long_signal and not short_signal:
        return None
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    return _make_signal(df, cfg, direction, "sma_cross", curr.get("sma_fast", 0), curr.get("sma_slow", 0), curr["volume"], curr["volume_ma"])


# 3. RSI bounce
def get_signal_rsi_bounce(df: pd.DataFrame, cfg: Config,
                          htf_trend: Optional[str] = None) -> Optional[Signal]:
    if cfg.rsi_period <= 0 or len(df) < cfg.rsi_period + 2:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("rsi")) or pd.isna(curr.get("rsi")):
        return None
    rsi_curr = float(curr["rsi"])
    rsi_prev = float(prev["rsi"])
    long_signal = rsi_prev < cfg.rsi_low and rsi_curr > rsi_prev
    short_signal = rsi_prev > cfg.rsi_high and rsi_curr < rsi_prev
    if not long_signal and not short_signal:
        return None
    _sig_bump("rsi_bounce")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "rsi_bounce_long" if direction == "LONG" else "rsi_bounce_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 4. RSI divergence
def get_signal_rsi_divergence(df: pd.DataFrame, cfg: Config,
                              htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 20 or not _safe_series(df, "rsi"):
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("rsi")) or pd.isna(curr.get("rsi")):
        return None
    window = df.iloc[-6:-1]
    if len(window) < 3:
        return None
    price_min_idx = window["close"].idxmin()
    price_max_idx = window["close"].idxmax()
    rsi_at_min = float(window.loc[price_min_idx, "rsi"]) if not pd.isna(window.loc[price_min_idx, "rsi"]) else 0.0
    rsi_at_max = float(window.loc[price_max_idx, "rsi"]) if not pd.isna(window.loc[price_max_idx, "rsi"]) else 0.0
    curr_close = float(curr["close"])
    prev_close = float(prev["close"])
    curr_rsi = float(curr["rsi"])
    long_signal = curr_close < prev_close and curr_close < window["close"].min() and curr_rsi > rsi_at_min
    short_signal = curr_close > prev_close and curr_close > window["close"].max() and curr_rsi < rsi_at_max
    if not long_signal and not short_signal:
        return None
    _sig_bump("rsi_divergence")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "rsi_divergence_long" if direction == "LONG" else "rsi_divergence_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 5. MACD momentum
def get_signal_macd_momentum(df: pd.DataFrame, cfg: Config,
                             htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < cfg.macd_slow + 2:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("macd")) or pd.isna(prev.get("macd_signal")) or pd.isna(curr.get("macd")) or pd.isna(curr.get("macd_signal")):
        return None
    prev_macd = float(prev["macd"])
    prev_signal = float(prev["macd_signal"])
    curr_macd = float(curr["macd"])
    curr_signal = float(curr["macd_signal"])
    long_signal = prev_macd <= prev_signal and curr_macd > curr_signal
    short_signal = prev_macd >= prev_signal and curr_macd < curr_signal
    if not long_signal and not short_signal:
        return None
    _sig_bump("macd_momentum")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "macd_momentum_long" if direction == "LONG" else "macd_momentum_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 6. MACD histogram trend
def get_signal_macd_hist_trend(df: pd.DataFrame, cfg: Config,
                               htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 4 or not _safe_series(df, "macd_hist"):
        return None
    curr = df.iloc[-1]
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]
    if pd.isna(curr.get("macd_hist")) or pd.isna(prev.get("macd_hist")) or pd.isna(prev2.get("macd_hist")):
        return None
    hist_curr = float(curr["macd_hist"])
    hist_prev = float(prev["macd_hist"])
    hist_prev2 = float(prev2["macd_hist"])
    long_signal = hist_curr > hist_prev > hist_prev2 and hist_curr > 0
    short_signal = hist_curr < hist_prev < hist_prev2 and hist_curr < 0
    if not long_signal and not short_signal:
        return None
    _sig_bump("macd_hist_trend")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "macd_hist_trend_long" if direction == "LONG" else "macd_hist_trend_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 7. Bollinger Bounce
def get_signal_bb_bounce(df: pd.DataFrame, cfg: Config,
                         htf_trend: Optional[str] = None) -> Optional[Signal]:
    if cfg.bb_period <= 0 or len(df) < cfg.bb_period + 2:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("bb_lower")) or pd.isna(prev.get("bb_upper")) or pd.isna(curr.get("bb_lower")) or pd.isna(curr.get("bb_upper")):
        return None
    bb_lower = float(curr["bb_lower"])
    bb_upper = float(curr["bb_upper"])
    curr_close = float(curr["close"])
    prev_close = float(prev["close"])
    long_signal = prev_close <= bb_lower and curr_close > bb_lower
    short_signal = prev_close >= bb_upper and curr_close < bb_upper
    if not long_signal and not short_signal:
        return None
    _sig_bump("bb_bounce")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "bb_bounce_long" if direction == "LONG" else "bb_bounce_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 8. Bollinger Squeeze Breakout
def get_signal_bb_squeeze_breakout(df: pd.DataFrame, cfg: Config,
                                   htf_trend: Optional[str] = None) -> Optional[Signal]:
    if cfg.bb_period <= 0 or len(df) < cfg.bb_period + 5:
        return None
    prev = df.iloc[-2]
    prev2 = df.iloc[-3]
    curr = df.iloc[-1]
    if pd.isna(curr.get("bb_upper")) or pd.isna(prev.get("bb_upper")) or pd.isna(prev2.get("bb_upper")):
        return None
    bb_width_curr = float(curr["bb_upper"]) - float(curr["bb_lower"])
    bb_width_prev = float(prev["bb_upper"]) - float(prev["bb_lower"])
    bb_width_prev2 = float(prev2["bb_upper"]) - float(prev2["bb_lower"])
    squeeze = bb_width_curr < bb_width_prev < bb_width_prev2
    close_curr = float(curr["close"])
    close_prev = float(prev["close"])
    long_signal = squeeze and close_curr > float(curr["bb_upper"]) and close_prev <= float(prev["bb_upper"])
    short_signal = squeeze and close_curr < float(curr["bb_lower"]) and close_prev >= float(prev["bb_lower"])
    if not long_signal and not short_signal:
        return None
    _sig_bump("bb_squeeze_breakout")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "bb_squeeze_breakout_long" if direction == "LONG" else "bb_squeeze_breakout_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 9. Stochastic bounce
def get_signal_stoch_bounce(df: pd.DataFrame, cfg: Config,
                            htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 20 or not _safe_series(df, "stoch_k") or not _safe_series(df, "stoch_d"):
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("stoch_k")) or pd.isna(curr.get("stoch_k")):
        return None
    k_prev = float(prev["stoch_k"])
    k_curr = float(curr["stoch_k"])
    d_curr = float(curr["stoch_d"]) if not pd.isna(curr.get("stoch_d")) else 0.0
    long_signal = k_prev < 20 and k_curr > k_prev and k_curr > d_curr
    short_signal = k_prev > 80 and k_curr < k_prev and k_curr < d_curr
    if not long_signal and not short_signal:
        return None
    _sig_bump("stoch_bounce")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "stoch_bounce_long" if direction == "LONG" else "stoch_bounce_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 10. Stochastic OB/OS levels
def get_signal_stoch_obos(df: pd.DataFrame, cfg: Config,
                          htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 3 or not _safe_series(df, "stoch_k"):
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("stoch_k")) or pd.isna(curr.get("stoch_k")):
        return None
    k_prev = float(prev["stoch_k"])
    k_curr = float(curr["stoch_k"])
    long_signal = k_prev < 20 and k_curr >= 20
    short_signal = k_prev > 80 and k_curr <= 80
    if not long_signal and not short_signal:
        return None
    _sig_bump("stoch_obos")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "stoch_obos_long" if direction == "LONG" else "stoch_obos_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 11. VWAP return
def get_signal_vwap_return(df: pd.DataFrame, cfg: Config,
                           htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 20 or not _safe_series(df, "vwap"):
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("vwap")) or pd.isna(curr.get("vwap")):
        return None
    vwap_prev = float(prev["vwap"])
    vwap_curr = float(curr["vwap"])
    close_prev = float(prev["close"])
    close_curr = float(curr["close"])
    long_signal = close_prev < vwap_prev and close_curr > vwap_curr
    short_signal = close_prev > vwap_prev and close_curr < vwap_curr
    if not long_signal and not short_signal:
        return None
    _sig_bump("vwap_return")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "vwap_return_long" if direction == "LONG" else "vwap_return_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 12. VWAP band
def get_signal_vwap_band(df: pd.DataFrame, cfg: Config,
                         htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 10 or not _safe_series(df, "vwap"):
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("vwap")) or pd.isna(curr.get("vwap")):
        return None
    vwap_curr = float(curr["vwap"])
    close_curr = float(curr["close"])
    close_prev = float(prev["close"])
    deviation_pct = abs(close_prev - vwap_curr) / vwap_curr * 100
    significant_deviation = deviation_pct > 1.0
    long_signal = close_prev < vwap_curr and significant_deviation and close_curr > vwap_curr
    short_signal = close_prev > vwap_curr and significant_deviation and close_curr < vwap_curr
    if not long_signal and not short_signal:
        return None
    _sig_bump("vwap_band")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "vwap_band_long" if direction == "LONG" else "vwap_band_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 13. ATR breakout
def get_signal_atr_breakout(df: pd.DataFrame, cfg: Config,
                            htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 20 or not _safe_series(df, "atr"):
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("atr")) or pd.isna(curr.get("atr")):
        return None
    atr_curr = float(curr["atr"])
    close_curr = float(curr["close"])
    high_prev = float(prev["high"])
    low_prev = float(prev["low"])
    breakout_long = close_curr > high_prev + atr_curr
    breakout_short = close_curr < low_prev - atr_curr
    if not breakout_long and not breakout_short:
        return None
    _sig_bump("atr_breakout")
    direction = "LONG" if breakout_long else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "atr_breakout_long" if direction == "LONG" else "atr_breakout_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 14. ATR trailing
def get_signal_atr_trailing(df: pd.DataFrame, cfg: Config,
                            htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 5 or not _safe_series(df, "atr"):
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("atr")) or pd.isna(curr.get("atr")):
        return None
    atr_curr = float(curr["atr"])
    atr_prev = float(prev["atr"])
    atr_increasing = atr_curr > atr_prev
    close_curr = float(curr["close"])
    high_prev = float(prev["high"])
    low_prev = float(prev["low"])
    long_signal = close_curr > high_prev + 0.5 * atr_curr and atr_increasing
    short_signal = close_curr < low_prev - 0.5 * atr_curr and atr_increasing
    if not long_signal and not short_signal:
        return None
    _sig_bump("atr_trailing")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "atr_trailing_long" if direction == "LONG" else "atr_trailing_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 15. Volume spike
def get_signal_volume_spike(df: pd.DataFrame, cfg: Config,
                            htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 2 or not _safe_series(df, "volume_ma"):
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(curr["volume_ma"]) or pd.isna(prev["volume_ma"]):
        return None
    vol_spike = curr["volume"] >= 2.0 * curr["volume_ma"]
    price_up = float(curr["close"]) > float(prev["close"])
    price_down = float(curr["close"]) < float(prev["close"])
    long_signal = vol_spike and price_up
    short_signal = vol_spike and price_down
    if not long_signal and not short_signal:
        return None
    _sig_bump("volume_spike")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "volume_spike_long" if direction == "LONG" else "volume_spike_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 16. Volume trend
def get_signal_volume_trend(df: pd.DataFrame, cfg: Config,
                            htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < cfg.volume_ma_period + 2 or not _safe_series(df, "volume_ma"):
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(curr["volume_ma"]) or pd.isna(prev["volume_ma"]):
        return None
    vol_up = float(curr["volume_ma"]) > float(prev["volume_ma"])
    price_up = float(curr["close"]) > float(prev["close"])
    long_signal = vol_up and price_up
    short_signal = not vol_up and not price_up
    if not long_signal and not short_signal:
        return None
    _sig_bump("volume_trend")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "volume_trend_long" if direction == "LONG" else "volume_trend_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 17. Supertrend
def get_signal_supertrend(df: pd.DataFrame, cfg: Config,
                          htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 12 or not _safe_series(df, "supertrend"):
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("supertrend")) or pd.isna(curr.get("supertrend")):
        return None
    st_prev = float(prev["supertrend"])
    st_curr = float(curr["supertrend"])
    close_prev = float(prev["close"])
    close_curr = float(curr["close"])
    long_signal = close_prev < st_prev and close_curr > st_curr
    short_signal = close_prev > st_prev and close_curr < st_curr
    if not long_signal and not short_signal:
        return None
    _sig_bump("supertrend")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "supertrend_long" if direction == "LONG" else "supertrend_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 18. Supertrend re-entry
def get_signal_supertrend_reentry(df: pd.DataFrame, cfg: Config,
                                  htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 15 or not _safe_series(df, "supertrend"):
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("supertrend")) or pd.isna(curr.get("supertrend")):
        return None
    window = df.iloc[-4:-1]
    if len(window) < 3:
        return None
    st_vals = window["supertrend"].dropna()
    closes = window["close"].dropna()
    if len(st_vals) < 2 or len(closes) < 2:
        return None
    was_uptrend = all(closes > st_vals)
    was_downtrend = all(closes < st_vals)
    st_curr = float(curr["supertrend"])
    close_prev = float(prev["close"])
    close_curr = float(curr["close"])
    long_signal = was_uptrend and close_prev <= st_curr and close_curr > st_curr
    short_signal = was_downtrend and close_prev >= st_curr and close_curr < st_curr
    if not long_signal and not short_signal:
        return None
    _sig_bump("supertrend_reentry")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "supertrend_reentry_long" if direction == "LONG" else "supertrend_reentry_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 19. ADX trend strength
def get_signal_adx_trend(df: pd.DataFrame, cfg: Config,
                         htf_trend: Optional[str] = None) -> Optional[Signal]:
    if cfg.adx_threshold <= 0 or len(df) < cfg.adx_period + 2:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("adx")) or pd.isna(curr.get("adx")):
        return None
    if pd.isna(prev.get("+di")) or pd.isna(curr.get("+di")) or pd.isna(prev.get("-di")) or pd.isna(curr.get("-di")):
        return None
    adx_curr = float(curr["adx"])
    if adx_curr < cfg.adx_threshold:
        return None
    plus_di_prev = float(prev["+di"])
    plus_di_curr = float(curr["+di"])
    minus_di_prev = float(prev["-"])
    minus_di_curr = float(curr["-di"])
    long_signal = plus_di_prev <= minus_di_prev and plus_di_curr > minus_di_curr
    short_signal = minus_di_prev <= plus_di_prev and minus_di_curr > plus_di_curr
    if not long_signal and not short_signal:
        return None
    _sig_bump("adx_trend")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "adx_trend_long" if direction == "LONG" else "adx_trend_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 20. Ichimoku cloud
def get_signal_ichimoku(df: pd.DataFrame, cfg: Config,
                        htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 52:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("ichimoku_senkou_a")) or pd.isna(prev.get("ichimoku_senkou_b")):
        return None
    if pd.isna(curr.get("ichimoku_senkou_a")) or pd.isna(curr.get("ichimoku_senkou_b")):
        return None
    close_curr = float(curr["close"])
    senkou_a_curr = float(curr["ichimoku_senkou_a"])
    senkou_b_curr = float(curr["ichimoku_senkou_b"])
    tenkan_curr = float(curr["ichimoku_tenkan"]) if not pd.isna(curr.get("ichimoku_tenkan")) else 0.0
    kijun_curr = float(curr["ichimoku_kijun"]) if not pd.isna(curr.get("ichimoku_kijun")) else 0.0
    cloud_top = max(senkou_a_curr, senkou_b_curr)
    cloud_bottom = min(senkou_a_curr, senkou_b_curr)
    long_signal = close_curr > cloud_top and tenkan_curr > kijun_curr
    short_signal = close_curr < cloud_bottom and tenkan_curr < kijun_curr
    if not long_signal and not short_signal:
        return None
    _sig_bump("ichimoku")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "ichimoku_long" if direction == "LONG" else "ichimoku_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 21. Ichimoku TK cross
def get_signal_ichimoku_tk_cross(df: pd.DataFrame, cfg: Config,
                                 htf_trend: Optional[str] = None) -> Optional[Signal]:
    if len(df) < 26:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    if pd.isna(prev.get("ichimoku_tenkan")) or pd.isna(prev.get("ichimoku_kijun")):
        return None
    if pd.isna(curr.get("ichimoku_tenkan")) or pd.isna(curr.get("ichimoku_kijun")):
        return None
    if pd.isna(curr.get("ichimoku_senkou_a")) or pd.isna(curr.get("ichimoku_senkou_b")):
        return None
    tenkan_prev = float(prev["ichimoku_tenkan"])
    kijun_prev = float(prev["ichimoku_kijun"])
    tenkan_curr = float(curr["ichimoku_tenkan"])
    kijun_curr = float(curr["ichimoku_kijun"])
    senkou_a = float(curr["ichimoku_senkou_a"])
    senkou_b = float(curr["ichimoku_senkou_b"])
    cloud_top = max(senkou_a, senkou_b)
    cloud_bottom = min(senkou_a, senkou_b)
    close_curr = float(curr["close"])
    within_cloud = cloud_bottom <= close_curr <= cloud_top
    long_signal = tenkan_prev <= kijun_prev and tenkan_curr > kijun_curr and within_cloud
    short_signal = tenkan_prev >= kijun_prev and tenkan_curr < kijun_curr and within_cloud
    if not long_signal and not short_signal:
        return None
    _sig_bump("ichimoku_tk")
    direction = "LONG" if long_signal else "SHORT"
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    preset = "ichimoku_tk_cross_long" if direction == "LONG" else "ichimoku_tk_cross_short"
    return _make_signal(df, cfg, direction, preset, curr.get("ema_fast", 0), curr.get("ema_slow", 0), curr.get("volume", 0), curr.get("volume_ma", 0))


# 22. Morning Star
def detect_candlestick_patterns(df: pd.DataFrame) -> Optional[tuple[str, str]]:
    if len(df) < 3:
        return None
    c0 = df.iloc[-3]
    c1 = df.iloc[-2]
    c2 = df.iloc[-1]
    o0, c0c = float(c0["open"]), float(c0["close"])
    o1, c1c = float(c1["open"]), float(c1["close"])
    o2, c2c = float(c2["open"]), float(c2["close"])
    body0 = abs(c0c - o0)
    body1 = abs(c1c - o1)
    body2 = abs(c2c - o2)
    if body0 == 0 or body1 == 0 or body2 == 0:
        return None
    if c0c < o0 and body1 < body0 * 0.5 and float(c1["low"]) < float(c0["low"]) and c2c > o2 and c2c > (o0 + c0c) / 2:
        return "morning_star", "LONG"
    if c0c > o0 and body1 < body0 * 0.5 and float(c1["high"]) > float(c0["high"]) and c2c < o2 and c2c < (o0 + c0c) / 2:
        return "evening_star", "SHORT"
    return None


def get_signal_candlestick(df: pd.DataFrame, cfg: Config,
                           htf_trend: Optional[str] = None) -> Optional[Signal]:
    pattern = detect_candlestick_patterns(df)
    if pattern is None:
        return None
    pattern_name, direction = pattern
    curr = df.iloc[-1]
    _sig_bump("pattern")
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    return _make_signal(df, cfg, direction, pattern_name,
                        curr.get("ema_fast", 0), curr.get("ema_slow", 0),
                        curr.get("volume", 0), curr.get("volume_ma", 0))


# 23. Engulfing
def detect_engulfing_pattern(df: pd.DataFrame) -> Optional[tuple[str, str]]:
    if len(df) < 2:
        return None
    prev = df.iloc[-2]
    curr = df.iloc[-1]
    o_prev, c_prev = float(prev["open"]), float(prev["close"])
    o_curr, c_curr = float(curr["open"]), float(curr["close"])
    prev_body = abs(c_prev - o_prev)
    curr_body = abs(c_curr - o_curr)
    if prev_body == 0 or curr_body == 0:
        return None
    if c_prev < o_prev and c_curr > o_curr and o_curr < c_prev and c_curr > o_prev:
        return "engulfing_long"
    if c_prev > o_prev and c_curr < o_curr and o_curr > c_prev and c_curr < o_prev:
        return "engulfing_short"
    return None


def get_signal_engulfing(df: pd.DataFrame, cfg: Config,
                         htf_trend: Optional[str] = None) -> Optional[Signal]:
    pattern = detect_engulfing_pattern(df)
    if pattern is None:
        return None
    pattern_name, direction = pattern
    curr = df.iloc[-1]
    _sig_bump("pattern")
    if cfg.htf_enabled and htf_trend is not None and htf_trend != direction:
        _sig_bump("htf")
        return None
    _sig_bump("pass")
    return _make_signal(df, cfg, direction, pattern_name,
                        curr.get("ema_fast", 0), curr.get("ema_slow", 0),
                        curr.get("volume", 0), curr.get("volume_ma", 0))


# ── Main entry point ──────────────────────────────────────────────────────

def get_signal(
    df: pd.DataFrame,
    cfg: Config,
    htf_trend: Optional[str] = None,
    enabled_presets: Optional[list[str]] = None,
) -> Optional[Signal]:
    signals = get_all_signals(df, cfg, htf_trend, enabled_presets)
    if not signals:
        return None
    signals.sort(key=lambda s: s.volume, reverse=True)
    return signals[0]


def get_all_signals(
    df: pd.DataFrame,
    cfg: Config,
    htf_trend: Optional[str] = None,
    enabled_presets: Optional[list[str]] = None,
) -> list[Signal]:
    if enabled_presets is None:
        enabled_presets = ["ema_cross_long", "ema_cross_short"]
    from preset_config import get_enabled_presets
    presets = get_enabled_presets(enabled_presets)
    results: list[Signal] = []
    seen_directions = set()
    preset_funcs = {
        "ema_cross_long": get_signal_ema_cross,
        "ema_cross_short": get_signal_ema_cross,
        "sma_cross_long": get_signal_sma_cross,
        "sma_cross_short": get_signal_sma_cross,
        "rsi_bounce_long": get_signal_rsi_bounce,
        "rsi_bounce_short": get_signal_rsi_bounce,
        "rsi_divergence_long": get_signal_rsi_divergence,
        "rsi_divergence_short": get_signal_rsi_divergence,
        "macd_momentum_long": get_signal_macd_momentum,
        "macd_momentum_short": get_signal_macd_momentum,
        "macd_hist_trend_long": get_signal_macd_hist_trend,
        "macd_hist_trend_short": get_signal_macd_hist_trend,
        "bb_bounce_long": get_signal_bb_bounce,
        "bb_bounce_short": get_signal_bb_bounce,
        "bb_squeeze_breakout_long": get_signal_bb_squeeze_breakout,
        "bb_squeeze_breakout_short": get_signal_bb_squeeze_breakout,
        "stoch_bounce_long": get_signal_stoch_bounce,
        "stoch_bounce_short": get_signal_stoch_bounce,
        "stoch_obos_long": get_signal_stoch_obos,
        "stoch_obos_short": get_signal_stoch_obos,
        "vwap_return_long": get_signal_vwap_return,
        "vwap_return_short": get_signal_vwap_return,
        "vwap_band_long": get_signal_vwap_band,
        "vwap_band_short": get_signal_vwap_band,
        "atr_breakout_long": get_signal_atr_breakout,
        "atr_breakout_short": get_signal_atr_breakout,
        "atr_trailing_long": get_signal_atr_trailing,
        "atr_trailing_short": get_signal_atr_trailing,
        "volume_spike_long": get_signal_volume_spike,
        "volume_spike_short": get_signal_volume_spike,
        "volume_trend_long": get_signal_volume_trend,
        "volume_trend_short": get_signal_volume_trend,
        "supertrend_long": get_signal_supertrend,
        "supertrend_short": get_signal_supertrend,
        "supertrend_reentry_long": get_signal_supertrend_reentry,
        "supertrend_reentry_short": get_signal_supertrend_reentry,
        "adx_trend_long": get_signal_adx_trend,
        "adx_trend_short": get_signal_adx_trend,
        "ichimoku_long": get_signal_ichimoku,
        "ichimoku_short": get_signal_ichimoku,
        "ichimoku_tk_cross_long": get_signal_ichimoku_tk_cross,
        "ichimoku_tk_cross_short": get_signal_ichimoku_tk_cross,
        "morning_star": get_signal_candlestick,
        "evening_star": get_signal_candlestick,
        "engulfing_long": get_signal_engulfing,
        "engulfing_short": get_signal_engulfing,
    }
    for preset_name in presets:
        func = preset_funcs.get(preset_name)
        if func is None:
            continue
        sig = func(df, cfg, htf_trend)
        if sig is not None:
            sig.preset = preset_name
            if sig.direction not in seen_directions:
                results.append(sig)
                seen_directions.add(sig.direction)
    return results
