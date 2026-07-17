import asyncio
import logging
import os
import signal
import sys
import pandas as pd
from dotenv import load_dotenv

# Load KuCoin env
env_path = os.path.join(os.path.dirname(__file__), "config", "kucoin", ".env")
if os.path.exists(env_path):
    load_dotenv(env_path)

from kucoin_config import load_kucoin_config, KuCoinConfig
from kucoin_client import get_client, get_balance, get_price, place_order, place_tp_order

async def run_bot(symbol: str, timeframe: str, config: KuCoinConfig, log: logging.Logger):
    log.info(f"[KCOIN] Starting bot for {symbol} on {timeframe}")
    
    # Получаем текущую цену
    try:
        price = await get_price(symbol)
        log.info(f"[KCOIN] Current price: {price}")
    except Exception as e:
        log.error(f"[KCOIN] Failed to get price: {e}")
        return
    
    if config.mode == "paper":
        log.info(f"[KCOIN] PAPER MODE - would place order at {price}")
    else:
        try:
            balance = await get_balance()
            qty = balance * config.risk_pct / 100 / (price * config.sl_pct / 100)
            log.info(f"[KCOIN] LIVE MODE - placing {symbol} qty={qty:.6f}")
            await place_order(symbol, "buy", "market", qty)
            log.info(f"[KCOIN] Order placed successfully")
        except Exception as e:
            log.error(f"[KCOIN] Failed to place order: {e}")

async def main():
    symbol = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    timeframe = sys.argv[2] if len(sys.argv) > 2 else "5m"
    
    cfg = load_kucoin_config()
    cfg.symbol = symbol
    cfg.timeframe = timeframe
    
    log = logging.getLogger("kucoin")
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    log.addHandler(handler)
    
    await run_bot(symbol, timeframe, cfg, log)

if __name__ == "__main__":
    asyncio.run(main())