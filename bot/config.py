import dataclasses
import os
import yaml
from dataclasses import dataclass, field
from typing import Optional


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
    htf_enabled: bool = False          # Включить старший ТФ-фильтр (основной)
    htf_timeframe: str = "1h"          # Старший ТФ (основной): 1h | 4h | 1d ...
    htf_ema_fast: int = 12
    htf_ema_slow: int = 26
    htf2_enabled: bool = False         # Включить второй старший ТФ-фильтр
    htf2_timeframe: str = "15m"        # Старший ТФ (второй): 15m | 1h ...
    htf2_ema_fast: int = 12
    htf2_ema_slow: int = 26
    recovery_enabled: bool = True
    recovery_max_position_pct: float = 100.0
    fixed_qty: float = 0.0          # Fixed position size in coins (0 = use risk_pct of balance)
    margin_pct: float = 0.0         # % от депозита на маржу: margin = round(balance*pct/100, 1); position = margin*leverage (0 = disabled)
    fixed_notional_usd: float = 0.0 # Fixed MARGIN (collateral) in USD; position = margin * leverage (0 = disabled)
    fixed_risk_usd: float = 0.0     # Fixed loss in USD at SL (0 = use risk_pct of balance)
    trade_mode: str = "manual"      # manual = обычная логика стратегий; auto = последовательности (заглушка)
    position_size_usd: float = 0.0  # МАРЖА в USD (0 = выкл): позиция = margin * leverage.
                                    # Приоритетнее margin_pct/fixed_*/risk_pct; глобальный дефолт live — LIVE_DEFAULT_MARGIN_USD.
    position_size_pct: float = 1.0  # % СВОБОДНОГО депозита (availableBalance) на маржу — только live (BOT_ENV=live).
                                    # margin = free_balance * pct/100; позиция = margin * leverage.
                                    # Приоритет: ниже position_size_usd, выше LIVE_DEFAULT_MARGIN_USD/margin_pct.
                                    # 0 = выкл. По умолчанию 1%.
    max_position_notional_usd: float = 0.0  # Потолок нотионала позиции, USD (0 = выкл). Жёсткий cap поверх любого сайзинга.
    max_position_pct_equity: float = 0.0    # Потолок нотионала как % от equity (с нереализованным PnL) (0 = выкл).
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
    llm_model: str = "minimax/minimax-m3:free"
    llm_fallback_models: str = ""
    llm_confidence_threshold: float = 0.7
    llm_calls_per_min: int = 20
    llm_per_symbol_cooldown_min: int = 5
    llm_backoff_sec: float = 60.0
    llm_short_backoff_sec: float = 5.0
    llm_provider_retry_delay_sec: float = 1.0
    gemini_api_key: str = ""
    gemini_model: str = "gemini-flash-lite-latest"
    groq_api_key: str = ""
    groq_model: str = "groq/compound-mini"
    commission_pct: float = 0.05   # Симулируемая комиссия (Taker) в %, применяется к PnL в paper/backtest
    taker_fee_pct: float = 0.05    # Taker-комиссия в % (вход новой ноги market + консервативный выход).
                                   # Используется в unified fee-aware reverse-сайзинге (order_manager).
    maker_fee_pct: float = 0.02    # Maker-комиссия в % (справочно: лимитный выход TP). В сайзинге не участвует.
    reverse_profit_pct: float = 0.1  # Целевой профит сверх безубытка в % от notional выхода (0 = ровно ноль).
                                     # Входит в unified reverse-сайзинг как p.
    use_fixed_tp_sl: bool = False  # True = использовать фиксированные sl_pct/tp1_pct из пресета/конфига
                                   # (БЕЗ динамического ATR SL/TP). Для честного теста 1.2%/0.4% на бэктесте.
    preset_sl_pct: Optional[float] = None  # Переопределение SL пресета в % (None = использовать PRESET_CONFIG)
    preset_tp_pct: Optional[float] = None  # Переопределение TP пресета в % (None = использовать PRESET_CONFIG)
    exchange_sl_backstop_enabled: bool = True  # True = держать на бирже ШИРОКИЙ STOP_MARKET (safety-net на случай офлайна/бана)
                                               # сверх виртуального SL. Виртуальный SL (reverse-стратегия) НЕ трогается.
    exchange_sl_backstop_pct: float = 2.0      # Насколько backstop шире виртуального SL: LONG trigger = sl*(1-pct/100),
                                               # SHORT trigger = sl*(1+pct/100). 2.0 = на 2% дальше уровня SL.
    reverse_breakeven_pct: float = 0.5     # % от SL, на который ставится TP обратной ноги от уровня SL:
                                           # LONG TP = sl*(1-pct/100), SHORT TP = sl*(1+pct/100). Хедж сайзится
                                           # ровно в безубыток: Qh = Qo * |E - P3| / |S - P3|.
    reverse_sl_pct: float = 1.0            # Виртуальный SL обратной ноги от её входа: reverse SHORT
                                           # sl = E_rev*(1+pct/100), reverse LONG sl = E_rev*(1-pct/100).
    reverse_chain_max: int = 10            # Макс. шагов reverse за цикл. 0 = БЕЗ ЛИМИТА (добавляем
                                            # ногу сколько нужно). step >= max → нет нового плеча, force-close.
    reverse_cum_loss_pct: float = 5.0      # Принудительно закрыть цикл, если суммарный убыток
                                            # (realized legs + текущий unrealized) >= этого % от референса.
                                            # 0 = выкл.
    reverse_cum_ref_deposit_usd: float = 0.0  # Референс для cumulative loss cap в USD.
                                            # 0 = текущий equity/депозит автоматически.
    loss_streak_skip_enabled: bool = True  # False = НЕ пропускать сигналы после серии убытков
                                           # (skip:loss_streak_3/5/7).
    reverse_fee_buffer_pct: float = 0.0   # Опциональная ДОП. маржа к окну безубытка сверх явных комиссий:
                                           # w = reverse_breakeven_pct + reverse_fee_buffer_pct.
                                           # Комиссии теперь учитываются явно (taker_fee_pct) в unified
                                           # сайзинге, поэтому по умолчанию 0.0.
    atr_tp_multiplier: float = 2.0         # RR для ATR-стопов: TP = atr_tp_multiplier * SL (2.0 = RR 2:1)
    atr_tp_multiplier_long: Optional[float] = None  # Переопределение RR для LONG (None = atr_tp_multiplier)
    atr_tp_multiplier_short: Optional[float] = None  # Переопределение RR для SHORT (None = atr_tp_multiplier)
    atr_tp2_multiplier: float = 0.0        # TP2 (раннер) = atr_tp2_multiplier * SL. 0 = TP2 совпадает с TP1 (по умолч.)
    min_consensus: int = 1        # Минимум РАЗНЫХ стратегий, согласных на направление (1 = выкл)
    consensus_flat: Optional[int] = None   # consensus при ADX<15 (None = min_consensus)
    consensus_weak: Optional[int] = None   # consensus при ADX 15-25 (None = min_consensus)
    consensus_trend: Optional[int] = None  # consensus при ADX>=25 (None = min_consensus)
    trend_block_short: bool = False        # True = не открывать SHORT при ADX>=25 (тренд)
    block_hours_utc: list = field(default_factory=list)  # часы UTC, в которые НЕ входить (напр. [2,6,10])
    excluded_presets: list = field(default_factory=list)  # пресеты, сигналы которых не открывать
    entry_on: str = "close"       # "close" = вход по close свечи сигнала; "next_open" = по open след. свечи

    def __post_init__(self):
        valid_modes = ("live", "backtest")
        if self.mode not in valid_modes:
            raise ValueError(f"mode must be one of {valid_modes}, got: {self.mode}")
        if self.trade_mode not in ("manual", "auto"):
            raise ValueError(f"trade_mode must be 'manual' or 'auto', got: {self.trade_mode}")
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


# --------------------------------------------------------------------------- #
#  Reverse-chain helpers: ступенчатая цель и виртуальный SL по шагу цепочки.
# --------------------------------------------------------------------------- #
def _reverse_step_doubling() -> bool:
    return os.getenv("REVERSE_STEP_DOUBLING", "false").strip().lower() == "true"


def reverse_w_pct_for_step(cfg: "Config", step: int) -> float:
    """Цель обратной ноги в % (breakeven target).

    Если включён REVERSE_STEP_DOUBLING (testnet): шаги 0 и 1 — базовый
    reverse_breakeven_pct; начиная с 3-го (step>=2) цель удваивается каждый шаг:
    0.5, 0.5, 1, 2, 4, 8 … Иначе — всегда базовое значение (live без изменений).
    """
    base = float(getattr(cfg, "reverse_breakeven_pct", 0.5) or 0.0)
    if not _reverse_step_doubling():
        return base
    s = int(step or 0)
    return base if s <= 1 else base * (2 ** (s - 1))


def reverse_sl_pct_for_step(cfg: "Config", step: int) -> float:
    """Виртуальный SL обратной ноги в % .

    При REVERSE_STEP_DOUBLING (testnet, вариант B): SL = цель + 0.3, чтобы TP
    оставался достижимым (P(TP) ~ 50%+). Иначе — обычный cfg.reverse_sl_pct.
    """
    if not _reverse_step_doubling():
        return float(getattr(cfg, "reverse_sl_pct", 1.0) or 0.0)
    return reverse_w_pct_for_step(cfg, step) + 0.3
