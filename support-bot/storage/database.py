import aiosqlite
import os
from datetime import datetime
from typing import Optional

from models.user import User
from models.api_key import ApiKey


class Database:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    async def init(self):
        async with aiosqlite.connect(self.path) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    created_at TEXT NOT NULL
                )
            """)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    symbol TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    encrypted_key TEXT NOT NULL,
                    encrypted_secret TEXT NOT NULL,
                    iv TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL,
                    last_used_at TEXT,
                    FOREIGN KEY(user_id) REFERENCES users(id)
                )
            """)
            await db.commit()

    async def get_or_create_user(self, telegram_id: int, username: Optional[str], first_name: Optional[str], last_name: Optional[str]) -> User:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    return User(
                        id=row["id"],
                        telegram_id=row["telegram_id"],
                        username=row["username"],
                        first_name=row["first_name"],
                        last_name=row["last_name"],
                        created_at=row["created_at"],
                    )
            now = datetime.utcnow().isoformat()
            async with db.execute(
                "INSERT INTO users (telegram_id, username, first_name, last_name, created_at) VALUES (?, ?, ?, ?, ?)",
                (telegram_id, username, first_name, last_name, now),
            ) as cursor:
                user_id = cursor.lastrowid
            await db.commit()
            return User(id=user_id, telegram_id=telegram_id, username=username, first_name=first_name, last_name=last_name, created_at=now)

    async def add_api_key(self, user_id: int, symbol: str, mode: str, encrypted_key: str, encrypted_secret: str, iv: str) -> ApiKey:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "INSERT INTO api_keys (user_id, symbol, mode, encrypted_key, encrypted_secret, iv, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
                (user_id, symbol, mode, encrypted_key, encrypted_secret, iv, now),
            ) as cursor:
                key_id = cursor.lastrowid
            await db.commit()
            return ApiKey(
                id=key_id, user_id=user_id, symbol=symbol, mode=mode,
                encrypted_key=encrypted_key, encrypted_secret=encrypted_secret, iv=iv,
                is_active=True, created_at=now, last_used_at=None,
            )

    async def get_user_keys(self, user_id: int) -> list[ApiKey]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM api_keys WHERE user_id = ?", (user_id,)) as cursor:
                rows = await cursor.fetchall()
                return [
                    ApiKey(
                        id=r["id"], user_id=r["user_id"], symbol=r["symbol"], mode=r["mode"],
                        encrypted_key=r["encrypted_key"], encrypted_secret=r["encrypted_secret"], iv=r["iv"],
                        is_active=bool(r["is_active"]), created_at=r["created_at"], last_used_at=r["last_used_at"],
                    )
                    for r in rows
                ]
