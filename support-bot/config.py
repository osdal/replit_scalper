from pydantic_settings import BaseSettings
from dotenv import load_dotenv
import os

load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))

class Settings(BaseSettings):
    support_telegram_bot_token: str = os.getenv("SUPPORT_BOT_TOKEN", "")
    support_chat_id: str = os.getenv("SUPPORT_CHAT_ID", "")
    master_key: str = os.getenv("SUPPORT_BOT_MASTER_KEY", "")
    support_bot_db: str = os.getenv("SUPPORT_BOT_DB", "data/support_bot.db")
    binance_base_url: str = "https://fapi.binance.com"

    model_config = {"env_file": os.path.join(os.path.dirname(__file__), "..", ".env"), "extra": "ignore"}

settings = Settings()
