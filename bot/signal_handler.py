import asyncio
import logging

from config import Config
from strategy import Signal


class SignalHandler:
    def __init__(self, cfg: Config, logger: logging.Logger):
        self.cfg = cfg
        self.log = logger

    async def confirm(self, signal: Signal) -> bool:
        """
        Returns True if the signal should be executed.
        In auto_mode — always True.
        In semi-auto — asks user confirmation via stdin.
        In backtest — always True (no interactive input).
        """
        msg = (
            f"Signal: {signal.direction} | entry={signal.entry_price} "
            f"SL={signal.sl_price} TP1={signal.tp1_price} TP2={signal.tp2_price}"
        )
        self.log.info(f"New signal received | {msg}")

        if self.cfg.mode == "backtest" or self.cfg.auto_mode:
            return True

        print(f"\n{'=' * 60}")
        print(f"  {msg}")
        print(f"  Confirm trade? [y/n]: ", end="", flush=True)

        loop = asyncio.get_event_loop()
        answer = await loop.run_in_executor(None, input)
        answer = answer.strip().lower()

        if answer in ("y", "yes", "да", "д"):
            self.log.info("Signal confirmed by user")
            return True
        else:
            self.log.info("Signal rejected by user")
            return False
