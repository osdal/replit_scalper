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
class UserCredentials:
    id: int
    user_id: int
    encrypted_api_key: str
    encrypted_api_secret: str
    iv: str
    is_active: bool
    created_at: str
    last_used_at: Optional[str]
