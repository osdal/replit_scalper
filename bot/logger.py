import logging
import os
from logging.handlers import RotatingFileHandler

_logger = None


def get_logger(log_file: str = "logs/bot.log", mode: str = "paper") -> logging.Logger:
    global _logger
    if _logger is not None:
        return _logger

    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    fmt = f"%(asctime)s [{mode.upper()}] %(levelname)s %(message)s"
    formatter = logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    logger = logging.getLogger("bot")
    logger.setLevel(logging.DEBUG)

    fh = RotatingFileHandler(log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    _logger = logger
    return logger
