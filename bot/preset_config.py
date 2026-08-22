"""
Per-preset конфигурация для replit_scalper.

Каждый пресет — это независимая торговая стратегия с собственными TP/SL/лимитами.
Функция get_preset_config() ищет конфигурацию:
  1. Точное совпадение по имени (momentum_long)
  2. Fallback по базовому имени + _long / _short (momentum → momentum_long / momentum_short)
  3. Глобальные дефолты из .env / config
"""

from typing import Optional

PRESET_CONFIG: dict[str, dict] = {
    # ── 1. EMA cross (основная стратегия) ──────────────────────────────────
    "ema_cross_long": {"tp": 1.0, "sl": 0.5, "max_per_preset": 0},
    "ema_cross_short": {"tp": 1.0, "sl": 0.5, "max_per_preset": 0},
    # ── 2. SMA cross (классический кросс SMA) ──────────────────────────────
    "sma_cross_long": {"tp": 1.0, "sl": 0.5, "max_per_preset": 0},
    "sma_cross_short": {"tp": 1.0, "sl": 0.5, "max_per_preset": 0},
    # ── 3. RSI bounce (отскок от перепроданности/перекупленности) ──────────
    "rsi_bounce_long": {"tp": 0.8, "sl": 0.4, "max_per_preset": 0, "mode": "paper"},
    "rsi_bounce_short": {"tp": 0.8, "sl": 0.4, "max_per_preset": 0},
    # ── 4. RSI divergence (дивергенция цены и RSI) ────────────────────────
    "rsi_divergence_long": {"tp": 1.0, "sl": 0.5, "max_per_preset": 0},
    "rsi_divergence_short": {"tp": 1.0, "sl": 0.5, "max_per_preset": 0},
    # ── 5. MACD momentum (импульс по MACD) ────────────────────────────────
    "macd_momentum_long": {"tp": 1.1, "sl": 0.55, "max_per_preset": 0},
    "macd_momentum_short": {"tp": 1.1, "sl": 0.55, "max_per_preset": 0},
    # ── 6. MACD histogram trend (рост/падение гистограммы) ────────────────
    "macd_hist_trend_long": {"tp": 1.0, "sl": 0.5, "max_per_preset": 0},
    "macd_hist_trend_short": {"tp": 1.0, "sl": 0.5, "max_per_preset": 0},
    # ── 7. Bollinger Bounce (отскок от полос) ─────────────────────────────
    "bb_bounce_long": {"tp": 0.9, "sl": 0.45, "max_per_preset": 0},
    "bb_bounce_short": {"tp": 0.9, "sl": 0.45, "max_per_preset": 0},
    # ── 8. Bollinger Squeeze Breakout (сжатие + пробой) ───────────────────
    "bb_squeeze_breakout_long": {"tp": 1.2, "sl": 0.6, "max_per_preset": 0},
    "bb_squeeze_breakout_short": {"tp": 1.2, "sl": 0.6, "max_per_preset": 0},
    # ── 9. Stochastic bounce (отскок от перепроданности/перекупленности) ──
    "stoch_bounce_long": {"tp": 0.7, "sl": 0.35, "max_per_preset": 0},
    "stoch_bounce_short": {"tp": 0.7, "sl": 0.35, "max_per_preset": 0},
    # ── 10. Stochastic OB/OS (уровни 20/80) ───────────────────────────────
    "stoch_obos_long": {"tp": 0.8, "sl": 0.4, "max_per_preset": 0},
    "stoch_obos_short": {"tp": 0.8, "sl": 0.4, "max_per_preset": 0},
    # ── 11. VWAP return (возврат к VWAP после отклонения) ────────────────
    "vwap_return_long": {"tp": 0.9, "sl": 0.45, "max_per_preset": 0},
    "vwap_return_short": {"tp": 0.9, "sl": 0.45, "max_per_preset": 0},
    # ── 12. VWAP band (отбой от полосы VWAP) ──────────────────────────────
    "vwap_band_long": {"tp": 0.9, "sl": 0.45, "max_per_preset": 0},
    "vwap_band_short": {"tp": 0.9, "sl": 0.45, "max_per_preset": 0},
    # ── 13. ATR breakout (пробой волатильности) ──────────────────────────
    "atr_breakout_long": {"tp": 1.2, "sl": 0.6, "max_per_preset": 0},
    "atr_breakout_short": {"tp": 1.2, "sl": 0.6, "max_per_preset": 0},
    # ── 14. ATR trailing (следование за ATR) ──────────────────────────────
    "atr_trailing_long": {"tp": 1.1, "sl": 0.55, "max_per_preset": 0},
    "atr_trailing_short": {"tp": 1.1, "sl": 0.55, "max_per_preset": 0},
    # ── 15. Volume spike (всплеск объёма + направление цены) ─────────────
    "volume_spike_long": {"tp": 1.0, "sl": 0.5, "max_per_preset": 0},
    "volume_spike_short": {"tp": 1.0, "sl": 0.5, "max_per_preset": 0},
    # ── 16. Volume trend (тренд по объёму) ────────────────────────────────
    "volume_trend_long": {"tp": 0.9, "sl": 0.45, "max_per_preset": 0},
    "volume_trend_short": {"tp": 0.9, "sl": 0.45, "max_per_preset": 0},
    # ── 17. Supertrend (следование за трендом) ────────────────────────────
    "supertrend_long": {"tp": 1.3, "sl": 0.65, "max_per_preset": 0},
    "supertrend_short": {"tp": 1.3, "sl": 0.65, "max_per_preset": 0},
    # ── 18. Supertrend re-entry (повторный вход при смене тренда) ─────────
    "supertrend_reentry_long": {"tp": 1.2, "sl": 0.6, "max_per_preset": 0},
    "supertrend_reentry_short": {"tp": 1.2, "sl": 0.6, "max_per_preset": 0},
    # ── 19. ADX strength (сильный тренд по ADX + EMA) ────────────────────
    "adx_trend_long": {"tp": 1.2, "sl": 0.6, "max_per_preset": 0},
    "adx_trend_short": {"tp": 1.2, "sl": 0.6, "max_per_preset": 0},
    # ── 20. Ichimoku (стратегия на основе облака Ишимоку) ────────────────
    "ichimoku_long": {"tp": 1.1, "sl": 0.55, "max_per_preset": 0},
    "ichimoku_short": {"tp": 1.1, "sl": 0.55, "max_per_preset": 0},
    # ── 21. Ichimoku TK cross (кросс Tenkan/Kijun) ────────────────────────
    "ichimoku_tk_cross_long": {"tp": 1.0, "sl": 0.5, "max_per_preset": 0},
    "ichimoku_tk_cross_short": {"tp": 1.0, "sl": 0.5, "max_per_preset": 0},
    # ── 22. Morning Star (бычья свечная паттерн) ──────────────────────────
    "morning_star": {"tp": 1.2, "sl": 0.6, "max_per_preset": 0},
    # ── 23. Evening Star (медвежья свечная паттерн) ───────────────────────
    "evening_star": {"tp": 1.2, "sl": 0.6, "max_per_preset": 0},
    # ── 24. Engulfing (поглощающая свечная паттерн) ───────────────────────
    "engulfing_long": {"tp": 1.1, "sl": 0.55, "max_per_preset": 0},
    "engulfing_short": {"tp": 1.1, "sl": 0.55, "max_per_preset": 0},
}

DEFAULT_PRESET_CONFIG = {
    "tp": 1.0,
    "sl": 0.5,
    "max_per_preset": 0,
    "mode": None,  # None = наследует режим бота (cfg.mode); "paper"|"live" - переопределяет
}


def get_preset_config(preset_name: str) -> dict:
    """Возвращает конфиг пресета с fallback-логикой."""
    if preset_name in PRESET_CONFIG:
        return PRESET_CONFIG[preset_name]

    for suffix in ("_long", "_short"):
        candidate = preset_name + suffix
        if candidate in PRESET_CONFIG:
            return PRESET_CONFIG[candidate]

    return DEFAULT_PRESET_CONFIG.copy()


def get_enabled_presets(enabled_list: list[str]) -> list[str]:
    """Возвращает список активных пресетов, проверяя их наличие в PRESET_CONFIG."""
    if not enabled_list:
        return ["ema_cross_long", "ema_cross_short"]
    result = []
    for p in enabled_list:
        if p in PRESET_CONFIG:
            result.append(p)
        else:
            for suffix in ("_long", "_short"):
                if p + suffix in PRESET_CONFIG:
                    result.append(p + suffix)
    return result if result else ["ema_cross_long", "ema_cross_short"]
