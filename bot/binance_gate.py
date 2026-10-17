"""
Общая защита от IP-бана Binance (-1003 / -429) между всеми ботами (один файл на все
процессы, атомарная запись tmp+rename).

Состояние:  data/binance_gate.json
    {"ban_until_ms": int, "used_weight_1m": int, "ts": float}

Ворота на уровне AsyncClient: оборачиваем AsyncClient._request. Перед каждым
REST-запросом проверяем gate, после ответа пишем фактический использованный вес
(X-MBX-USED-WEIGHT-1M) и ban_until_ms при -1003.

Приоритеты:
  * КРИТИЧНЫЕ (ордера, отмена, закрытие позиции, backstop, sync при старте)
    проходят ВСЕГДА, кроме явного бана (-1003).
  * ВТОРОСТЕПЕННЫЕ (heartbeat, пыль, баланс для отчётов, klines-фолбэк, cum-cap)
    откладываются/пропускаются, когда used_weight_1m > BINANCE_SHED_AT или идёт бан.

Fail-open: если файл не читается/битый — работаем как раньше, пишем WARNING раз в
минуту. Раз в минуту пишем одну INFO-строку: used_weight_1m, сколько запросов
отложено.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger("binance_gate")# Вес по умолчанию, если заголовок X-MBX-USED-WEIGHT-1M недоступен.
DEFAULT_WEIGHT = 1

# Лимит веса Binance USD-M Futures (2400 weight/мин на IP).
WEIGHT_LIMIT_1M = int(os.getenv("BINANCE_WEIGHT_LIMIT_1M", "2400"))

# Доля лимита, выше которой второстепенные запросы откладываются.
SHED_AT = float(os.getenv("BINANCE_SHED_AT", "0.75"))

# Как часто писать INFO-строку со статистикой (сек).
STATS_INTERVAL_SEC = 60.0


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_sec() -> float:
    return time.time()


def _parse_ban_until(message: Optional[str]) -> Optional[int]:
    """Из '-1003 ... banned until 1787463454222' достаёт ms-таймстамп."""
    if not message:
        return None
    marker = "banned until "
    idx = message.find(marker)
    if idx < 0:
        return None
    rest = message[idx + len(marker):].strip()
    digits = ""
    for ch in rest:
        if ch.isdigit():
            digits += ch
        else:
            break
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


class AccountGate:
    """Общий шлюз ко всем REST-запросам Binance через общий JSON-файл."""

    def __init__(
        self,
        path: str | Path = Path("data/binance_gate.json"),
        weight_limit_1m: int = WEIGHT_LIMIT_1M,
        shed_at: float = SHED_AT,
    ) -> None:
        self.path = Path(path)
        self.weight_limit_1m = weight_limit_1m
        self.shed_at = shed_at
        self._last_stats_ts: float = 0.0
        self._deferred_count: int = 0

    # ------------------------------------------------------------------ #
    #  File helpers                                                      #
    # ------------------------------------------------------------------ #
    def _read(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not a dict")
            return data
        except Exception:
            return {"ban_until_ms": 0, "used_weight_1m": 0, "ts": 0.0}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.path) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, str(self.path))

    # ------------------------------------------------------------------ #
    #  Gate                                                              #
    # ------------------------------------------------------------------ #
    def is_banned(self) -> bool:
        data = self._read()
        return data.get("ban_until_ms", 0) > _now_ms()

    def check_and_reserve(
        self, weight: int = DEFAULT_WEIGHT, is_critical: bool = False
    ) -> tuple[bool, str]:
        """Проверяет gate и резервирует вес. Возвращает (allowed, reason).

        КРИТИЧНЫЕ запросы проходят всегда (кроме явного бана).
        ВТОРОСТЕПЕННЫЕ пропускаются, если used_weight_1m > shed_at или бан.
        """
        now = _now_sec()
        data = self._read()

        # --- Янтарный свет: явный бан --------------------------------------
        ban_until = data.get("ban_until_ms", 0)
        if ban_until > _now_ms():
            remaining = (ban_until - _now_ms()) / 1000.0
            return False, f"banned until {ban_until} ({remaining:.0f}s left)"

        # --- Жёлтый свет: превышен весовой лимит --------------------------
        total_used = data.get("used_weight_1m", 0)
        if total_used / self.weight_limit_1m > self.shed_at and not is_critical:
            self._deferred_count += 1
            self._maybe_log_stats()
            return False, f"shed {total_used}/{self.weight_limit_1m}"

        return True, "ok"

    def record_response(
        self, weight_used: int, ban_until_ms: Optional[int] = None
    ) -> None:
        """Записывает фактический использованный вес после ответа."""
        now = _now_sec()
        data = self._read()

        if ban_until_ms and ban_until_ms > 0:
            data["ban_until_ms"] = ban_until_ms

        # Сброс окна, если прошла минута
        if now - data.get("ts", 0.0) > 60.0:
            data["used_weight_1m"] = weight_used
        else:
            data["used_weight_1m"] = data.get("used_weight_1m", 0) + weight_used

        data["ts"] = now
        self._write(data)

    def record_ban(self, ban_until_ms: int) -> None:
        """Записывает время снятия бана в общий файл."""
        now = _now_sec()
        data = self._read()
        data["ban_until_ms"] = ban_until_ms
        data["ts"] = now
        self._write(data)

    def _maybe_log_stats(self) -> None:
        now = _now_sec()
        if now - self._last_stats_ts < STATS_INTERVAL_SEC:
            return
        self._last_stats_ts = now
        data = self._read()
        logger.warning(
            f"[RATE_LIMIT] used_weight_1m={data.get('used_weight_1m', 0)}/"
            f"{self.weight_limit_1m}, deferred={self._deferred_count}, "
            f"banned_until={data.get('ban_until_ms', 0)}"
        )

    def get_status(self) -> dict:
        data = self._read()
        return {
            "ban_until_ms": data.get("ban_until_ms", 0),
            "used_weight_1m": data.get("used_weight_1m", 0),
            "weight_limit_1m": self.weight_limit_1m,
            "shed_at": self.shed_at,
            "ts": data.get("ts", 0.0),
        }


# --------------------------------------------------------------------------- #
#  Wrapper для AsyncClient._request                                         #
# --------------------------------------------------------------------------- #
def _estimate_weight(method: str, uri: str) -> int:
    """Приблизительный вес запроса в единицах Binance weight."""
    u = uri.upper()
    # Тяжёлые эндпоинты
    if "/ACCOUNT" in u:
        return 5
    if "/POSITIONRISK" in u:
        return 5
    if "/ALLORDERS" in u or "/OPENORDERS" in u:
        return 5
    if "/MYTRADES" in u:
        return 5
    if "/KLINES" in u:
        return 1
    if "/TICKER/PRICE" in u:
        return 1
    if "/TICKER/BOOKTICKER" in u:
        return 2
    if "/DEPTH" in u:
        return 5
    # Ордера и отмены
    if method.upper() in ("POST", "PUT", "DELETE"):
        return 1
    return DEFAULT_WEIGHT


def _is_critical(method: str, uri: str) -> bool:
    """Критические запросы: ордера, отмена, закрытие позиции, backstop, sync."""
    u = uri.upper()
    if method.upper() in ("POST", "DELETE", "PUT"):
        # Ордера, отмена ордеров, algo-ордера
        if any(k in u for k in ("ORDER", "ALGOORDER")):
            return True
    if "/POSITIONRISK" in u or "/ACCOUNT" in u:
        # Баланс/позиции — критичны для стартовой синхронизации
        return True
    return False


def wrap_client(client, gate: AccountGate):
    """Обёртка вокруг AsyncClient._request: проверяет gate перед каждым
    REST-запросом, пишет использованный вес и ban_until_ms после ответа.

    Никаких изменений в вызывающем коде — только обёртка метода _request.
    """
    original_request = client._request

    async def gated_request(method, uri, signed, force_params=False, **kwargs):
        weight = _estimate_weight(method, uri)
        is_critical = _is_critical(method, uri)

        allowed, reason = gate.check_and_reserve(weight, is_critical=is_critical)
        if not allowed:
            # Поднимаем обычное исключение с -429 в тексте — caller обработает
            # его как отказ rate-limit.
            raise Exception(f"gate shed: {reason} (code -429)")

        try:
            response = await original_request(
                method, uri, signed, force_params, **kwargs
            )
        except Exception as e:
            code = getattr(e, 'code', 0)
            if code == -1003:
                ban_until = _parse_ban_until(str(e))
                if ban_until:
                    gate.record_ban(ban_until)
            raise
        except Exception:
            raise

        # Записываем фактический вес из заголовка ответа
        used_weight = int(
            response.headers.get("X-MBX-USED-WEIGHT-1M", weight)  # type: ignore
        )
        gate.record_response(used_weight)
        return response

    client._request = gated_request
    return client
