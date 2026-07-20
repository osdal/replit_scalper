import dataclasses
import yaml
from dataclasses import dataclass

@dataclass
class KuCoinConfig:
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
    mode: str = "paper"
    auto_mode: bool = True
    backtest_start: str = ""
    backtest_end: str = ""
    paper_balance: float = 1000.0
    log_file: str = "logs/kucoin.log"
    htf_enabled: bool = False
    htf_timeframe: str = "1h"
    htf_ema_fast: int = 9
    htf_ema_slow: int = 21
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""

def load_kucoin_config(path: str = "../config/kucoin/config.yaml") -> KuCoinConfig:
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    valid_fields = {f.name for f in dataclasses.fields(KuCoinConfig)}
    filtered = {k: v for k, v in data.items() if k in valid_fields}
    filtered.setdefault('api_key', os.getenv('KUCOIN_API_KEY', ''))
    filtered.setdefault('api_secret', os.getenv('KUCOIN_API_SECRET', ''))
    filtered.setdefault('api_passphrase', os.getenv('KUCOIN_API_PASSPHRASE', ''))
    return KuCoinConfig(**filtered)

import os