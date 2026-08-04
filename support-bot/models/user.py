from dataclasses import dataclass
from typing import Optional


@dataclass
class User:
    id: int
    telegram_id: int
    username: Optional[str]
    first_name: Optional[str]
    last_name: Optional[str]
    created_at: str


@dataclass
class ApiKey:
    id: int
    user_id: int
    symbol: str
    mode: str
    encrypted_key: str
    encrypted_secret: str
    iv: str
    is_active: bool
    created_at: str
    last_used_at: Optional[str]
