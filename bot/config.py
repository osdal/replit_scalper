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
        if self.tp1_pct >= self.tp2_pct:
            raise ValueError("tp1_pct must be less than tp2_pct")
        if not (0 < self.tp1_close_pct < 100):
            raise ValueError("tp1_close_pct must be between 0 and 100")
        if self.ema_fast >= self.ema_slow:
            raise ValueError("ema_fast must be less than ema_slow")
        if self.htf_enabled and self.htf_ema_fast >= self.htf_ema_slow:
            raise ValueError("htf_ema_fast must be less than htf_ema_slow")


def load_config(path: str = "config.yaml") -> Config:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    valid_fields = {f.name for f in dataclasses.fields(Config)}
    filtered = {k: v for k, v in data.items() if k in valid_fields}
    return Config(**filtered)
