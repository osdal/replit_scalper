from pydantic_settings import BaseSettings
from dotenv import load_dotenv
import os

load_dotenv()

class Settings(BaseSettings):
    telegram_bot_token: str = os.getenv("SUPPORT_BOT_TOKEN", "")
    support_chat_id: str = os.getenv("SUPPORT_CHAT_ID", "")
    master_key: str = os.getenv("SUPPORT_BOT_MASTER_KEY", "")
    database_path: str = os.getenv("SUPPORT_BOT_DB", "data/support_bot.db")
    binance_base_url: str = "https://fapi.binance.com"

    class Config:
        env_file = ".env"

settings = Settings()
