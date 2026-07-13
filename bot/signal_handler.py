import asyncio
import logging

from config import Config
from strategy import Signal

CONFIRM_TIMEOUT_SECONDS = 60


class SignalHandler:
    def __init__(self, cfg: Config, logger: logging.Logger):
        self.cfg = cfg
        self.log = logger

    async def confirm(self, signal: Signal) -> bool:
        """
        Returns True if the signal should be executed.
        In auto_mode — always True.
        In semi-auto — asks user confirmation via stdin with timeout.
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
        print(f"  Confirm trade? [y/n] (auto-skip in {CONFIRM_TIMEOUT_SECONDS}s): ", end="", flush=True)

        loop = asyncio.get_event_loop()
        try:
            answer = await asyncio.wait_for(
                loop.run_in_executor(None, input),
                timeout=CONFIRM_TIMEOUT_SECONDS,
            )
            answer = answer.strip().lower()
        except asyncio.TimeoutError:
            print()
            self.log.info(f"Signal timed out after {CONFIRM_TIMEOUT_SECONDS}s — skipped")
            return False

        if answer in ("y", "yes", "да", "д"):
            self.log.info("Signal confirmed by user")
            return True
        else:
            self.log.info("Signal rejected by user")
            return False
