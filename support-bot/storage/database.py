import aiosqlite
import os
from datetime import datetime
from typing import Optional

from models.user import User
from models.user_credentials import UserCredentials


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
                CREATE TABLE IF NOT EXISTS user_credentials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE NOT NULL,
                    encrypted_api_key TEXT NOT NULL,
                    encrypted_api_secret TEXT NOT NULL,
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

    async def upsert_credentials(self, user_id: int, encrypted_api_key: str, encrypted_api_secret: str, iv: str) -> UserCredentials:
        now = datetime.utcnow().isoformat()
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM user_credentials WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
            if row:
                await db.execute(
                    "UPDATE user_credentials SET encrypted_api_key=?, encrypted_api_secret=?, iv=?, is_active=1, created_at=? WHERE user_id=?",
                    (encrypted_api_key, encrypted_api_secret, iv, now, user_id),
                )
                creds_id = row["id"]
            else:
                async with db.execute(
                    "INSERT INTO user_credentials (user_id, encrypted_api_key, encrypted_api_secret, iv, is_active, created_at) VALUES (?, ?, ?, ?, 1, ?)",
                    (user_id, encrypted_api_key, encrypted_api_secret, iv, now),
                ) as cursor:
                    creds_id = cursor.lastrowid
            await db.commit()
            return UserCredentials(
                id=creds_id, user_id=user_id,
                encrypted_api_key=encrypted_api_key, encrypted_api_secret=encrypted_api_secret, iv=iv,
                is_active=True, created_at=now, last_used_at=None,
            )

    async def get_credentials(self, user_id: int) -> Optional[UserCredentials]:
        async with aiosqlite.connect(self.path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM user_credentials WHERE user_id = ?", (user_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return None
                return UserCredentials(
                    id=row["id"], user_id=row["user_id"],
                    encrypted_api_key=row["encrypted_api_key"], encrypted_api_secret=row["encrypted_api_secret"], iv=row["iv"],
                    is_active=bool(row["is_active"]), created_at=row["created_at"], last_used_at=row["last_used_at"],
                )

    async def deactivate_credentials(self, user_id: int) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute("UPDATE user_credentials SET is_active=0 WHERE user_id=?", (user_id,))
            await db.commit()
