#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Сервис регистрации пользователей бота через Telegram.

Особенности:
  * Только стандартная библиотека Python — не требует зависимостей и пересборки контейнеров.
  * Живёт на 127.0.0.1:5100, наружу отдаётся Caddy:
      - https://osdal.duckdns.org/bot-api/registration/{config,telegram}  (публичные)
      - https://scalper-bot.duckdns.org/bot-api/registration/*           (админка, за Basic auth)
  * Пишет в ту же БД, что и торговый API: /opt/trading-bot/data/bot_live.db
  * Конфиг (токен бота) читается на каждый запрос — перезапуск не нужен.
"""

import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    from profile import get_profile, render_profile_html
except Exception:  # noqa: BLE001
    import sys as _sys, os as _os
    _sys.path.insert(0, _os.path.dirname(os.path.abspath(__file__)))
    from profile import get_profile, render_profile_html

BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DB_PATH = os.environ.get("REGISTRATION_DB", os.path.join(BASE_DIR, "data", "bot_live.db"))
CONFIG_PATH = os.environ.get(
    "REGISTRATION_CONFIG", os.path.join(BASE_DIR, "data", "registration_config.json")
)
HOST = os.environ.get("REGISTRATION_HOST", "127.0.0.1")
PORT = int(os.environ.get("REGISTRATION_PORT", "5100"))

# Свежесть подписи Telegram (секунды). 1 сутки — с запасом на смену часового пояса.
MAX_AUTH_AGE_SEC = 24 * 60 * 60

ALLOWED_ORIGINS = {
    "https://osdal.duckdns.org",
    "https://scalper-bot.duckdns.org",
    "https://92-5-180-72.sslip.io",
    "http://localhost:5176",
    "http://127.0.0.1:5176",
}

STATUSES = {"active", "pending", "blocked"}


# ───────────────────────────── утилиты ──────────────────────────────

def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + \
        f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 15000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def table_columns(conn: sqlite3.Connection, table: str) -> set:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def ensure_schema() -> None:
    """Добавляет недостающие колонки в users (идемпотентно)."""
    additions = {
        "telegram_id": "INTEGER",
        "telegram_username": "TEXT",
        "first_name": "TEXT",
        "last_name": "TEXT",
        "photo_url": "TEXT",
        "phone": "TEXT",
        "notes": "TEXT",
        "status": "TEXT NOT NULL DEFAULT 'active'",
        "source": "TEXT NOT NULL DEFAULT 'telegram'",
        "last_login_at": "TEXT",
        "registered_via": "TEXT",
        "consent_messages": "INTEGER NOT NULL DEFAULT 0",
        "consent_at": "TEXT",
    }
    with connect() as conn:
        cols = table_columns(conn, "users")
        if not cols:
            raise RuntimeError(f"таблица users не найдена в {DB_PATH}")
        for name, ddl in additions.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_telegram_id "
            "ON users(telegram_id) WHERE telegram_id IS NOT NULL"
        )
        conn.commit()


def load_config() -> dict:
    """Токен/username бота: env имеет приоритет, иначе data/registration_config.json."""
    cfg = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh) or {}
    except FileNotFoundError:
        cfg = {}
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] не удалось прочитать {CONFIG_PATH}: {exc}", flush=True)
        cfg = {}

    token = os.environ.get("TELEGRAM_REG_BOT_TOKEN") or cfg.get("bot_token") or ""
    username = os.environ.get("TELEGRAM_REG_BOT_USERNAME") or cfg.get("bot_username") or ""
    name = cfg.get("bot_name") or ""
    return {
        "bot_token": token.strip(),
        "bot_username": username.strip().lstrip("@"),
        "bot_name": name.strip(),
    }


def get_default_gift() -> float:
    """Стартовый подарок новым клиентам (USD). Хранится в registration_config.json."""
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh) or {}
        value = float(cfg.get("default_gift_usd") or 0)
        return round(max(0.0, min(value, 100_000.0)), 4)
    except Exception:  # noqa: BLE001
        return 0.0


def set_default_gift(value: float) -> float:
    """Задаёт стартовый подарок новым клиентам (только админский эндпоинт)."""
    amount = round(float(value), 4)
    if amount < 0 or amount > 100_000:
        raise ValueError("Подарок должен быть от 0 до 100000 USD")
    cfg = {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            cfg = json.load(fh) or {}
    except FileNotFoundError:
        pass
    cfg["default_gift_usd"] = amount
    with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return amount


# ───────────────────────── Telegram login widget ─────────────────────────

def verify_telegram_payload(payload: dict, bot_token: str) -> tuple[bool, str]:
    """Проверка подписи Telegram Login Widget."""
    if not bot_token:
        return False, "Бот ещё не настроен: нет токена"
    check_hash = str(payload.get("hash") or "")
    if not check_hash:
        return False, "Отсутствует hash"

    # Telegram добавил в колбэк поле `consent` (подтверждение request_access=write),
    # но НЕ включает его в подпись. Исключаем и его, и служебный hash.
    # Значения приводим к строке: JSON-булевы (_fields consent) и числа должны
    # давать тот же вид, что в query-строке виджета.
    ignored = {"hash", "consent"}
    data_check_string = "\n".join(
        f"{key}={payload[key]}"
        for key in sorted(payload)
        if key not in ignored and payload[key] is not None
    )
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    expected = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, check_hash):
        # Диагностика: логируем БЕЗ секретов — только имена полей, их значения
        # (кроме hash) и обе подписи, чтобы понять, что именно прислал виджет.
        fields = {k: v for k, v in payload.items() if k != "hash"}
        print(
            f"[SIGN MISMATCH] fields={fields!r} dcs={data_check_string!r}\n"
            f"  expected={expected}\n  received={check_hash}",
            flush=True,
        )
        return False, "Подпись Telegram не совпала"

    try:
        auth_date = int(payload.get("auth_date") or 0)
    except (TypeError, ValueError):
        return False, "Некорректная дата авторизации"
    if auth_date <= 0 or abs(time.time() - auth_date) > MAX_AUTH_AGE_SEC:
        return False, "Данные авторизации устарели — обновите страницу"

    try:
        telegram_id = int(payload.get("id") or 0)
    except (TypeError, ValueError):
        return False, "Некорректный telegram id"
    if telegram_id <= 0:
        return False, "Некорректный telegram id"

    return True, ""


def telegram_api(bot_token: str, method: str, payload: dict, timeout: int = 10):
    """Вызов Bot API. Возвращает (ok, result_dict, error_code, description)."""
    if not bot_token:
        return False, {}, 0, "no token"
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/{method}",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return bool(body.get("ok")), body.get("result", {}), 0, ""
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8"))
        except Exception:  # noqa: BLE001
            body = {}
        desc = body.get("description", str(exc))
        print(f"[WARN] telegram {method}: {exc.code} {desc}", flush=True)
        return False, {}, exc.code, desc
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] telegram {method}: {exc}", flush=True)
        return False, {}, 0, str(exc)


def send_telegram_message(bot_token: str, chat_id: int, text: str,
                          reply_markup: dict | None = None) -> tuple[bool, bool]:
    """Отправка сообщения. Возвращает (ok, blocked_by_user).

    При 403 (пользователь заблокировал бота) вызывающий код должен пометить
    пользователя blocked и больше не слать.
    """
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    ok, _, code, desc = telegram_api(bot_token, "sendMessage", payload)
    blocked = (code == 403) or ("blocked" in (desc or "").lower())
    if blocked:
        mark_user_blocked(chat_id)
    return ok, blocked


def mark_user_blocked(telegram_id: int) -> None:
    """Пометить пользователя заблокированным (403 от Telegram)."""
    try:
        with connect() as conn:
            conn.execute(
                "UPDATE users SET status='blocked', consent_messages=0, updated_at=? "
                "WHERE telegram_id=?",
                (utc_now(), telegram_id),
            )
            conn.commit()
        print(f"[INFO] user {telegram_id} marked blocked (403)", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] mark_user_blocked: {exc}", flush=True)


def unsubscribe_keyboard(telegram_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "🔕 Отписаться от уведомлений", "callback_data": f"unsub:{telegram_id}"}
    ]]}


def subscribe_keyboard(telegram_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "🔔 Подписаться на уведомления", "callback_data": f"sub:{telegram_id}"}
    ]]}


# ─────────────────────────── бизнес-логика ───────────────────────────

def user_row_to_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "email": row["email"],
        "role": row["role"],
        "status": row["status"],
        "source": row["source"],
        "telegram_id": row["telegram_id"],
        "telegram_username": row["telegram_username"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "photo_url": row["photo_url"],
        "notes": row["notes"],
        "client_id": (
            int(re.search(r"client_id=(\d+)", row["notes"] or "").group(1))
            if re.search(r"client_id=(\d+)", row["notes"] or "")
            else None
        ),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "last_login_at": row["last_login_at"],
        "consent_messages": bool(row["consent_messages"]) if "consent_messages" in row.keys() else False,
        "consent_at": row["consent_at"] if "consent_at" in row.keys() else None,
        "profile_slug": (row["telegram_username"] or ("tg" + str(row["id"]))),
        "profile_url": "/u/" + (row["telegram_username"] or ("tg" + str(row["id"]))),
        "balance_usd": float(row["balance_usd"] or 0),
        "hwm_usd": float(row["hwm_usd"] or 0),
        "bots_count": int(row["bots_count"] or 0),
    }


def register_telegram_user(payload: dict) -> dict:
    cfg = load_config()
    ok, err = verify_telegram_payload(payload, cfg["bot_token"])
    if not ok:
        raise ValueError(err)

    telegram_id = int(payload["id"])
    username = (payload.get("username") or "").strip() or None
    first_name = (payload.get("first_name") or "").strip() or None
    last_name = (payload.get("last_name") or "").strip() or None
    photo_url = (payload.get("photo_url") or "").strip() or None
    consent = bool(payload.get("consent"))
    now = utc_now()

    with connect() as conn:
        existing = conn.execute(
            "SELECT id FROM users WHERE telegram_id = ?", (telegram_id,)
        ).fetchone()

        if existing:
            user_id = existing["id"]
            conn.execute(
                """UPDATE users SET telegram_username = COALESCE(?, telegram_username),
                                    first_name = COALESCE(?, first_name),
                                    last_name = COALESCE(?, last_name),
                                    photo_url = COALESCE(?, photo_url),
                                    last_login_at = ?, updated_at = ?,
                                    consent_messages = CASE WHEN ? THEN 1 ELSE consent_messages END,
                                    consent_at = CASE WHEN ? AND consent_at IS NULL THEN ? ELSE consent_at END
                   WHERE id = ?""",
                (username, first_name, last_name, photo_url, now, now,
                 1 if consent else 0, 1 if consent else 0, now, user_id),
            )
            already = True
        else:
            email = f"tg{telegram_id}@telegram.local"
            cur = conn.execute(
                """INSERT INTO users
                       (email, password_hash, role, created_at, updated_at,
                        telegram_id, telegram_username, first_name, last_name, photo_url,
                        status, source, last_login_at, registered_via,
                        consent_messages, consent_at)
                   VALUES (?, '', 'buyer', ?, ?, ?, ?, ?, ?, ?, 'active', 'telegram', ?, 'telegram-widget',
                           ?, ?)""",
                (email, now, now, telegram_id, username, first_name, last_name, photo_url, now,
                 1 if consent else 0, now if consent else None),
            )
            user_id = int(cur.lastrowid)
            conn.execute(
                """INSERT INTO user_internal_accounts
                       (user_id, balance_usd, high_water_mark_usd, created_at, updated_at)
                   VALUES (?, 0, 0, ?, ?)
                   ON CONFLICT(user_id) DO NOTHING""",
                (user_id, now, now),
            )
            # Дублируем в legacy-модель clients (используется торговым ядром/будущей биллинг-логикой).
            gift = get_default_gift()
            try:
                cur = conn.execute(
                    """INSERT INTO clients
                           (email, password_hash, role, status, fee_pct, gift_usd,
                            telegram_chat_id, auto_resume, notes, created_at, updated_at)
                       VALUES (?, '', 'client', 'active', 10, ?, ?, 1, ?, ?, ?)""",
                    (email, gift, str(telegram_id), "Зарегистрирован через Telegram", now, now),
                )
                client_id = int(cur.lastrowid)
                conn.execute(
                    """INSERT INTO client_accounts
                           (client_id, balance_usd, cum_pnl_usd, hwm_pnl_usd, updated_at)
                       VALUES (?, ?, 0, 0, ?)
                       ON CONFLICT(client_id) DO NOTHING""",
                    (client_id, gift, now),
                )
                if gift > 0:
                    conn.execute(
                        """INSERT INTO ledger
                               (client_id, type, amount_usd, balance_after_usd,
                                cum_pnl_after_usd, reason, actor, idempotency_key, created_at)
                           VALUES (?, 'gift', ?, ?, 0, ?, 'registration', ?, ?)""",
                        (client_id, gift, gift, "Подарок при регистрации",
                         f"gift:client:{client_id}", now),
                    )
                conn.execute(
                    "UPDATE users SET notes = ? WHERE id = ?", (f"client_id={client_id}", user_id)
                )
            except sqlite3.Error as exc:
                print(f"[WARN] legacy clients: {exc}", flush=True)
            already = False

        conn.commit()
        row = conn.execute(
            """SELECT u.*, COALESCE(a.balance_usd, 0) AS balance_usd,
                      COALESCE(a.high_water_mark_usd, 0) AS hwm_usd,
                      (SELECT COUNT(*) FROM bots b WHERE b.user_id = u.id) AS bots_count
               FROM users u LEFT JOIN user_internal_accounts a ON a.user_id = u.id
               WHERE u.id = ?""",
            (user_id,),
        ).fetchone()

    user = user_row_to_dict(row)

    if not already and consent:
        slug = user.get("profile_slug") or ("tg" + str(user["id"]))
        send_telegram_message(
            cfg["bot_token"],
            telegram_id,
            "✅ Регистрация на торговой платформе завершена.\n"
            "Ваш аккаунт активен.\n\n"
            "Ваша страница: https://osdal.duckdns.org/u/" + slug + "\n"
            "Уведомления можно отключить кнопкой ниже в любой момент.",
            reply_markup=unsubscribe_keyboard(telegram_id),
        )

    return {"user": user, "already_registered": already}


def list_users(limit: int = 500) -> list:
    with connect() as conn:
        rows = conn.execute(
            """SELECT u.*, COALESCE(a.balance_usd, 0) AS balance_usd,
                      COALESCE(a.high_water_mark_usd, 0) AS hwm_usd,
                      (SELECT COUNT(*) FROM bots b WHERE b.user_id = u.id) AS bots_count
               FROM users u LEFT JOIN user_internal_accounts a ON a.user_id = u.id
               ORDER BY u.created_at DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [user_row_to_dict(r) for r in rows]


def cleanup_orphan_accounts() -> int:
    """Удаляет client_accounts, у которых нет соответствующей записи в clients.

    Такие «сироты» остаются после неудачных/отменённых регистраций: счёт
    создаётся раньше, чем клиент, или клиент удаляется отдельно. На работу
    не влияют, но засоряют БД и мешают отладке. Вызывается при старте сервиса
    и после каждого удаления пользователя.
    """
    with connect() as conn:
        cur = conn.execute(
            """DELETE FROM client_accounts
               WHERE client_id NOT IN (SELECT id FROM clients)"""
        )
        removed = cur.rowcount
        conn.commit()
    return removed


def delete_user(user_id: int) -> dict:
    """Удаляет зарегистрированного пользователя и её платформенный счёт.

    Защита:
      * владелец платформы (role='owner') не удаляется;
      * пользователь с привязанными ботами не удаляется — сначала отвяжите ботов.
    Удаляются legacy-таблицы пользователя и его дубль в clients/client_accounts
    (client_id берём из notes или по email).
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT id, email, role, notes FROM users WHERE id = ?", (user_id,)
        ).fetchone()
        if not row:
            raise LookupError("Пользователь не найден")
        if row["role"] == "owner":
            raise ValueError("Нельзя удалить владельца платформы")

        bots = conn.execute(
            "SELECT COUNT(*) FROM bots WHERE user_id = ?", (user_id,)
        ).fetchone()[0]
        if bots:
            raise ValueError(
                f"У пользователя {bots} ботов — сначала отвяжите их, затем удаляйте"
            )

        client_id = None
        match = re.search(r"client_id=(\d+)", row["notes"] or "")
        if match:
            client_id = int(match.group(1))
        else:
            client_row = conn.execute(
                "SELECT id FROM clients WHERE email = ?", (row["email"],)
            ).fetchone()
            if client_row:
                client_id = int(client_row["id"])

        conn.execute("DELETE FROM user_internal_accounts WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM internal_transactions WHERE user_id = ?", (user_id,))
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        if client_id:
            conn.execute("DELETE FROM ledger WHERE client_id = ?", (client_id,))
            conn.execute("DELETE FROM client_accounts WHERE client_id = ?", (client_id,))
            conn.execute("DELETE FROM clients WHERE id = ?", (client_id,))
        conn.commit()

    # Подстраховка: убираем любые счета без клиента (в т.ч. от старых тестов).
    try:
        cleanup_orphan_accounts()
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] cleanup orphans: {exc}", flush=True)

    return {"deleted": True, "user_id": user_id, "client_id": client_id}


def stats() -> dict:
    with connect() as conn:
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        tg = conn.execute("SELECT COUNT(*) FROM users WHERE telegram_id IS NOT NULL").fetchone()[0]
        active = conn.execute("SELECT COUNT(*) FROM users WHERE status = 'active'").fetchone()[0]
        blocked = conn.execute("SELECT COUNT(*) FROM users WHERE status = 'blocked'").fetchone()[0]
        today = conn.execute(
            "SELECT COUNT(*) FROM users WHERE created_at >= ?",
            (datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00"),),
        ).fetchone()[0]
        balance = conn.execute(
            "SELECT COALESCE(SUM(balance_usd), 0) FROM user_internal_accounts"
        ).fetchone()[0]
    return {
        "total": total,
        "telegram": tg,
        "active": active,
        "blocked": blocked,
        "registered_today": today,
        "total_balance_usd": round(float(balance), 4),
    }


def update_user_status(user_id: int, status: str) -> dict:
    if status not in STATUSES:
        raise ValueError("Недопустимый статус")
    with connect() as conn:
        existing = conn.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not existing:
            raise LookupError("Пользователь не найден")
        conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
            (status, utc_now(), user_id),
        )
        conn.commit()
    return {"ok": True, "id": user_id, "status": status}


# ───────────────────────────── HTTP-слой ─────────────────────────────

class Handler(BaseHTTPRequestHandler):
    server_version = "RegistrationService/1.0"
    protocol_version = "HTTP/1.1"

    # --- helpers ---
    def _cors(self) -> None:
        origin = self.headers.get("Origin", "")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, x-admin-token")

    def _json(self, code: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if len(raw) > 64 * 1024:
            raise ValueError("Слишком большой запрос")
        return json.loads(raw.decode("utf-8") or "{}")

    def log_message(self, fmt, *args):  # компактный лог
        print(f"[{self.log_date_time_string()}] {self.address_string()} {fmt % args}", flush=True)

    # --- verbовки ---
    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        try:
            if path in ("/health", "/registration/health"):
                return self._json(200, {"ok": True, "service": "registration"})

            if path == "/registration/widget.js":
                js_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "widget.js")
                with open(js_path, "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-cache")
                self._cors()
                self.end_headers()
                self.wfile.write(body)
                return

            if path == "/registration/config":
                cfg = load_config()
                return self._json(200, {
                    "ready": bool(cfg["bot_token"] and cfg["bot_username"]),
                    "bot_username": cfg["bot_username"],
                    "bot_name": cfg["bot_name"],
                    "platform": "OSDAL Scalper",
                })

            if path == "/registration/settings":
                return self._json(200, {"default_gift_usd": get_default_gift()})

            if path == "/registration/stats":
                return self._json(200, stats())

            if path == "/registration/users":
                qs = parse_qs(parsed.query)
                limit = min(int(qs.get("limit", ["500"])[0]), 2000)
                return self._json(200, {"users": list_users(limit)})

            # публичный профиль (JSON)
            if path.startswith("/registration/profile/"):
                slug = path[len("/registration/profile/"):].strip("/")
                prof = get_profile(slug)
                if prof is None:
                    return self._json(404, {"error": "Пользователь не найден"})
                return self._json(200, prof)

            # страница пользователя /u/<slug>
            if path.startswith("/u/"):
                slug = path[len("/u/"):].strip("/")
                if not slug:
                    return self._json(400, {"error": "Укажите пользователя"})
                prof = get_profile(slug)
                html = render_profile_html(prof, slug)
                body = html.encode("utf-8")
                self.send_response(200 if prof else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self._cors()
                self.end_headers()
                self.wfile.write(body)
                return

            return self._json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json(500, {"error": str(exc)})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/registration/telegram":
                payload = self._read_json()
                try:
                    result = register_telegram_user(payload)
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                return self._json(200, {"ok": True, **result})

            if path == "/registration/notify":
                body = self._read_json()
                tg_id = body.get("telegram_id")
                text = (body.get("text") or "").strip()
                if not tg_id or not text:
                    return self._json(400, {"error": "Нужны telegram_id и text"})
                ok, status = notify_user(int(tg_id), text)
                return self._json(200 if ok else 400, {"ok": ok, "status": status})

            if path == "/registration/broadcast":
                body = self._read_json()
                text = (body.get("text") or "").strip()
                if not text:
                    return self._json(400, {"error": "Нужен text"})
                result = notify_all(text)
                return self._json(200, {"ok": True, **result})

            return self._json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json(500, {"error": str(exc)})

    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            parts = path.split("/")
            if len(parts) == 4 and parts[1] == "registration" and parts[2] == "users":
                try:
                    res = delete_user(int(parts[3]))
                except LookupError as exc:
                    return self._json(404, {"error": str(exc)})
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                return self._json(200, {"ok": True, **res})
            return self._json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json(500, {"error": str(exc)})

    def do_PATCH(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if path == "/registration/settings":
                body = self._read_json()
                try:
                    gift = set_default_gift(body.get("default_gift_usd", 0))
                except (TypeError, ValueError) as exc:
                    return self._json(400, {"error": str(exc)})
                return self._json(200, {"ok": True, "default_gift_usd": gift})

            parts = path.split("/")
            if len(parts) == 4 and parts[1] == "registration" and parts[2] == "users":
                body = self._read_json()
                try:
                    res = update_user_status(int(parts[3]), str(body.get("status") or ""))
                except ValueError as exc:
                    return self._json(400, {"error": str(exc)})
                except LookupError as exc:
                    return self._json(404, {"error": str(exc)})
                return self._json(200, res)
            return self._json(404, {"error": "not found"})
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return self._json(500, {"error": str(exc)})


def handle_update(update: dict) -> None:
    """Обработка одного апдейта от Telegram (polling)."""
    cfg = load_config()
    token = cfg["bot_token"]
    if not token:
        return

    # --- callback_query (кнопки inline) ---
    cq = update.get("callback_query")
    if cq:
        data = (cq.get("data") or "").strip()
        from_id = (cq.get("from") or {}).get("id")
        cq_id = cq.get("id")
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if data.startswith("unsub:") or data.startswith("sub:"):
            try:
                target_id = int(data.split(":", 1)[1])
            except (ValueError, IndexError):
                target_id = None
            if target_id is not None and from_id == target_id:
                new_consent = 1 if data.startswith("sub:") else 0
                with connect() as conn:
                    conn.execute(
                        "UPDATE users SET consent_messages=?, updated_at=? WHERE telegram_id=?",
                        (new_consent, utc_now(), target_id),
                    )
                    conn.commit()
                if new_consent:
                    text = "🔔 Вы снова подписаны на уведомления."
                    kb = unsubscribe_keyboard(target_id)
                else:
                    text = "🔕 Вы отписаны от уведомлений. Нажмите кнопку, чтобы подписаться снова."
                    kb = subscribe_keyboard(target_id)
                telegram_api(token, "answerCallbackQuery", {"callback_query_id": cq_id, "text": text[:200]})
                if chat_id:
                    telegram_api(token, "sendMessage", {
                        "chat_id": chat_id, "text": text, "reply_markup": kb,
                    })
            else:
                telegram_api(token, "answerCallbackQuery", {
                    "callback_query_id": cq_id, "text": "Нельзя изменить чужую подписку", "show_alert": True,
                })
        return

    # --- message ---
    msg = update.get("message") or {}
    text = (msg.get("text") or "").strip()
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    from_user = msg.get("from") or {}
    tg_id = from_user.get("id")
    if not (chat_id and tg_id and text):
        return

    if text.startswith("/start"):
        with connect() as conn:
            row = conn.execute(
                "SELECT status, consent_messages FROM users WHERE telegram_id=?", (tg_id,)
            ).fetchone()
        if row and row["status"] == "active":
            if row["consent_messages"]:
                telegram_api(token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": "Вы зарегистрированы и подписаны на уведомления.",
                    "reply_markup": unsubscribe_keyboard(tg_id),
                })
            else:
                telegram_api(token, "sendMessage", {
                    "chat_id": chat_id,
                    "text": "Вы зарегистрированы, но уведомления отключены.",
                    "reply_markup": subscribe_keyboard(tg_id),
                })
        else:
            telegram_api(token, "sendMessage", {
                "chat_id": chat_id,
                "text": "Чтобы зарегистрироваться, откройте страницу: https://osdal.duckdns.org/scalper-register/",
            })
    elif text.startswith("/stop") or text.startswith("/unsubscribe"):
        with connect() as conn:
            conn.execute("UPDATE users SET consent_messages=0, updated_at=? WHERE telegram_id=?",
                         (utc_now(), tg_id))
            conn.commit()
        telegram_api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "🔕 Уведомления отключены.",
            "reply_markup": subscribe_keyboard(tg_id),
        })
    elif text.startswith("/subscribe"):
        with connect() as conn:
            conn.execute("UPDATE users SET consent_messages=1, updated_at=? WHERE telegram_id=?",
                         (utc_now(), tg_id))
            conn.commit()
        telegram_api(token, "sendMessage", {
            "chat_id": chat_id,
            "text": "🔔 Уведомления включены.",
            "reply_markup": unsubscribe_keyboard(tg_id),
        })


_LAST_UPDATE_ID = 0


def poll_updates_loop() -> None:
    """Фоновый поток: опрос getUpdates, обработка callback_query и команд."""
    global _LAST_UPDATE_ID
    cfg = load_config()
    token = cfg["bot_token"]
    if not token:
        print("[POLL] токен не настроен — опрос отключён", flush=True)
        return
    # убедимся, что webhook снят
    telegram_api(token, "deleteWebhook", {"drop_pending_updates": False})
    print("[POLL] запущен", flush=True)
    while True:
        try:
            ok, result, _, _ = telegram_api(token, "getUpdates", {
                "offset": _LAST_UPDATE_ID + 1,
                "timeout": 25,
                "allowed_updates": ["message", "callback_query"],
            }, timeout=35)
            if ok and isinstance(result, list):
                for upd in result:
                    uid = upd.get("update_id", 0)
                    if uid > _LAST_UPDATE_ID:
                        _LAST_UPDATE_ID = uid
                    try:
                        handle_update(upd)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[POLL] handle_update: {exc}", flush=True)
        except Exception as exc:  # noqa: BLE001
            print(f"[POLL] error: {exc}", flush=True)
            time.sleep(5)


def notify_user(telegram_id: int, text: str) -> tuple[bool, str]:
    """Отправить сообщение конкретному пользователю (с проверкой согласия)."""
    cfg = load_config()
    token = cfg["bot_token"]
    with connect() as conn:
        row = conn.execute(
            "SELECT status, consent_messages FROM users WHERE telegram_id=?", (telegram_id,)
        ).fetchone()
    if not row:
        return False, "пользователь не найден"
    if row["status"] != "active":
        return False, f"статус {row['status']}"
    if not row["consent_messages"]:
        return False, "нет согласия на сообщения"
    ok, blocked = send_telegram_message(
        token, telegram_id, text, reply_markup=unsubscribe_keyboard(telegram_id),
    )
    if blocked:
        return False, "заблокирован пользователем"
    return ok, "ok" if ok else "ошибка отправки"


def notify_all(text: str) -> dict:
    """Рассылка всем согласившимся активным пользователям (с ограничением скорости)."""
    cfg = load_config()
    token = cfg["bot_token"]
    with connect() as conn:
        rows = conn.execute(
            "SELECT telegram_id FROM users WHERE status='active' AND consent_messages=1 AND telegram_id IS NOT NULL"
        ).fetchall()
    sent = 0
    blocked = 0
    failed = 0
    for r in rows:
        tg = r["telegram_id"]
        ok, status = notify_user(tg, text)
        if ok:
            sent += 1
        elif status == "заблокирован пользователем":
            blocked += 1
        else:
            failed += 1
        time.sleep(0.05)  # ~20 msg/sec
    return {"total": len(rows), "sent": sent, "blocked": blocked, "failed": failed}


def main() -> None:
    ensure_schema()
    try:
        removed = cleanup_orphan_accounts()
        if removed:
            print(f"[STARTUP] удалено {removed} сиротливых client_accounts", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[WARN] startup cleanup: {exc}", flush=True)
    cfg = load_config()
    # фоновый опрос Telegram (callback_query, /start, /stop)
    import threading
    threading.Thread(target=poll_updates_loop, daemon=True).start()
    print(
        f"registration service on http://{HOST}:{PORT} | db={DB_PATH} | "
        f"bot={'@' + cfg['bot_username'] if cfg['bot_username'] else 'НЕ НАСТРОЕН'}",
        flush=True,
    )
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    srv.daemon_threads = True
    srv.serve_forever()


if __name__ == "__main__":
    main()
