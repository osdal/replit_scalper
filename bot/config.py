import dataclasses
import yaml
from dataclasses import dataclass, field


@dataclass
class Config:
    symbol: str
    timeframe: str
    leverage: int
    risk_pct: float
    sl_pct: float
    tp1_pct: float
    tp1_close_pct: float
    tp2_pct: float
    ema_fast: int
    ema_slow: int
    volume_ma_period: int
    volume_multiplier: float
    mode: str
    auto_mode: bool
    backtest_start: str
    backtest_end: str
    paper_balance: float
    log_file: str
    htf_enabled: bool = False
    htf_timeframe: str = "1h"
    htf_ema_fast: int = 9
    htf_ema_slow: int = 21
    recovery_enabled: bool = True
    recovery_max_position_pct: float = 100.0
    fixed_qty: float = 0.0          # Fixed position size in coins (0 = use risk_pct of balance)
    margin_pct: float = 0.0         # % от депозита на маржу: margin = round(balance*pct/100, 1); position = margin*leverage (0 = disabled)
    fixed_notional_usd: float = 0.0 # Fixed MARGIN (collateral) in USD; position = margin * leverage (0 = disabled)
    fixed_risk_usd: float = 0.0     # Fixed loss in USD at SL (0 = use risk_pct of balance)
    adx_period: int = 14              # ADX период для расчёта силы тренда
    adx_threshold: float = 0.0        # ADX порог: сигналы при adx >= threshold (0 = фильтр отключён; тип. 20–25)
    time_profit_close_hours: float = 0.0  # Принудительно закрыть прибыльную позицию старше N часов (0 = выкл)
    max_open_per_cycle: int = 1       # Макс. новых позиций за цикл (0 = без лимита)
    signal_cooldown_min: int = 0      # Кулдаун между сигналами на один символ в минутах (0 = выкл)
    rsi_period: int = 14              # RSI период (0 = выкл)
    rsi_low: int = 30                 # RSI low threshold для LONG
    rsi_high: int = 70                # RSI high threshold для SHORT
    macd_fast: int = 12               # MACD fast EMA
    macd_slow: int = 26               # MACD slow EMA
    macd_signal: int = 9              # MACD signal line
    bb_period: int = 20               # Bollinger Bands период
    bb_std: float = 2.0               # Bollinger Bands стандартное отклонение
    enabled_presets: list[str] = None  # Список активных пресетов (None = только ema_cross)
    llm_enabled: bool = False         # Включить LLM проверку сигналов
    llm_mock: bool = False            # Мок-режим LLM (возвращает True всегда)
    llm_api_key: str = ""             # API ключ OpenRouter (основной)
    llm_model: str = "llama-3.1-70b-versatile"
    llm_fallback_models: str = ""
    llm_confidence_threshold: float = 0.7
    llm_calls_per_min: int = 20
    llm_per_symbol_cooldown_min: int = 5
    llm_backoff_sec: float = 60.0
    llm_short_backoff_sec: float = 5.0
    llm_provider_retry_delay_sec: float = 1.0
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash-exp"
    groq_api_key: str = ""
    groq_model: str = "groq/compound-mini"
    commission_pct: float = 0.05   # Симулируемая комиссия (Taker) в %, применяется к PnL в paper/backtest

    def __post_init__(self):
        valid_modes = ("live", "paper", "backtest")
        if self.mode not in valid_modes:
            raise ValueError(f"mode must be one of {valid_modes}, got: {self.mode}")
        if not (0 < self.risk_pct <= 100):
            raise ValueError("risk_pct must be between 0 and 100")
        if self.sl_pct <= 0:
            raise ValueError("sl_pct must be positive")
        if self.tp1_pct <= 0 or self.tp2_pct <= 0:
            raise ValueError("tp1_pct and tp2_pct must be positive")
        if self.tp1_pct > self.tp2_pct:
            raise ValueError("tp1_pct must be less than tp2_pct")
        if not (0 < self.tp1_close_pct <= 100):
            raise ValueError("tp1_close_pct must be between 0 and 100")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be less than ema_slow")
        if self.htf_enabled and self.htf_ema_fast >= self.htf_ema_slow:
            raise ValueError("htf_ema_fast must be less than htf_ema_slow")
        if self.enabled_presets is None:
            self.enabled_presets = ["ema_cross_long", "ema_cross_short"]


def load_config(path: str = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    valid_fields = {f.name for f in dataclasses.fields(Config)}
    filtered = {k: v for k, v in data.items() if k in valid_fields}
    return Config(**filtered)


def update_yaml_config(symbol: str, params: dict, bot_dir: str = ".") -> None:
    """Обновляет параметры в YAML-файле конфига бота."""
    config_path = f"{bot_dir}/config_{symbol.replace('USDT', '').lower()}.yaml"
    with open(config_path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    valid_fields = {f.name for f in dataclasses.fields(Config)}
    for key, value in params.items():
        if key in valid_fields:
            data[key] = value
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, default_flow_style=False, allow_unicode=True)
