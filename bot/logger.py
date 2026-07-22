import logging
import os
from logging.handlers import RotatingFileHandler

_loggers: dict = {}

TRADE_KEYWORDS = (
    "Position opened",
    "TP1 hit",
    "TP2 hit",
    "SL hit",
    "TP1_START",
    "TP1_RETURN",
    "Partial close",
    "Full close",
    "SL moved to breakeven",
    "[LIVE] Market order placed",
    "[LIVE] Partial close",
    "[LIVE] Full close",
    "[LIVE] Stop-loss placed",
    "[PAPER] Would open",
    "[PAPER] Would close partial",
    "[PAPER] Would close full",
    "[SYNC] Restored position",
    "[STATE] Restored from file",
    "[SYNC] Restored from exchange",
    "[RECOVERY]",
    "[RECOVERY_DEBUG]",
    "[RECOVERY][CLAIM_ERROR]",
    "[RECOVERY][REPORT_ERROR]",
    "[RECOVERY][LIMIT_EXCEEDED]",
    "[TP1_START]",
    "[TP1_RETURN]",
    "[TP2_HIT]",
    "[SL_HIT]",
    "[DEBUG]",
    "[CRITICAL]",
    "[PNL_SUMMARY]",
    "[PNL_MISMATCH]",
)


class TradeOnlyFilter(logging.Filter):
    """Пропускает в файл только сообщения о сделках и recovery."""
    def filter(self, record: logging.LogRecord) -> bool:
        return any(kw in record.getMessage() for kw in TRADE_KEYWORDS)


def get_logger(
    log_file: str = "logs/bot.log",
    mode: str = "paper",
    symbol: str = "",
) -> logging.Logger:
    if log_file in _loggers:
        return _loggers[log_file]

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    sym = f" {symbol.upper()}" if symbol else ""
    fmt = f"%(asctime)s [{mode.upper()}{sym}] %(levelname)s %(message)s"
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    logger = logging.getLogger(f"bot.{log_file}")
    logger.setLevel(logging.DEBUG)

    fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)
    fh.addFilter(TradeOnlyFilter())

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    _loggers[log_file] = logger
    return logger


def get_events_logger(symbol: str = "") -> logging.Logger:
    """Логгер для ключевых событий в отдельный файл events.log."""
    log_file = "logs/events.log"
    if log_file in _loggers:
        return _loggers[log_file]

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    fmt = "%(asctime)s [%(symbol)s] %(message)s"
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    logger = logging.getLogger("events")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    logger.addHandler(fh)
    _loggers[log_file] = logger
    return logger
