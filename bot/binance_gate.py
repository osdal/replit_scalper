"""
Общая защита от IP-бана Binance (-1003 / -429) между всеми ботами.

Каждый процесс пишет в СВОЙ файл <prefix>.<pid>.json. Читатели агрегируют все
файлы <path>.*: бан = максимум ban_until_ms, вес = самое свежее показание не
старше 60 с. Никакой общей записи из многих процессов в один файл.

Все операции с файлом обёрнуты в try/except — fail-open, WARNING при сбое.
Запись состояния не чаще раза в секунду на Prozess (бан — без задержки).
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional

from binance.exceptions import BinanceAPIException

logger = logging.getLogger("binance_gate")

DEFAULT_WEIGHT = 1
WEIGHT_LIMIT_1M = int(os.getenv("BINANCE_WEIGHT_LIMIT_1M", "2400"))
SHED_AT = float(os.getenv("BINANCE_SHED_AT", "0.75"))
STATS_INTERVAL_SEC = 60.0
WRITE_INTERVAL_SEC = 1.0
WEIGHT_TTL_SEC = 60.0
RETRY_AFTER_DEFAULT_SEC = 10.0


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


class GateBlocked(BinanceAPIException):
    """Запрос не отправлен в сеть. Наследуется от BinanceAPIException, чтобы
    существующий код (rate_limit.with_retry и except-ветки) вёл себя как при
    настоящем ответе Binance: бан -> code -1003, текст «banned until <ms>»;
    лимит веса -> code -429."""

    def __init__(self, reason: str, code: int = -429):
        status = 418 if code == -1003 else 429
        super().__init__(None, status, json.dumps({"code": code, "msg": f"gate blocked: {reason}"}))
        self.reason = reason


def _header(resp, name: str) -> Optional[str]:
    try:
        h = resp.headers
        return h.get(name) or h.get(name.lower())
    except Exception:
        return None


class AccountGate:
    """Общий шлюз ко всем REST-запросам Binance через файлы на диске.

    Каждый процесс пишет в свой файл <prefix>.<pid>.json. Читатели агрегируют
    все файлы <path>.*: бан = максимум ban_until_ms, вес = самое свежее
    показание не старше 60 с. Никакой общей записи из многих процессов в один файл.
    """

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
        self._last_write_ts: float = 0.0
        self._cache: dict | None = None
        self._cache_ts: float = 0.0

    # ------------------------------------------------------------------ #
    #  Файловые хелперы                                                  #
    # ------------------------------------------------------------------ #
    def _my_path(self) -> Path:
        """Путь к файлу этого процесса: <prefix>.<pid>.json"""
        return Path(f"{self.path}.{os.getpid()}")

    def _read_one(self, path: Path) -> dict:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not a dict")
            return data
        except Exception:
            return {"ban_until_ms": 0, "used_weight_1m": 0, "ts": 0.0}

    def _read_all(self) -> dict:
        """Агрегирует все файлы <path>.*: бан = максимум, вес = самое свежее."""
        best = {"ban_until_ms": 0, "used_weight_1m": 0, "ts": 0.0}
        try:
            for fpath in glob.glob(str(self.path) + "*"):
                try:
                    d = self._read_one(Path(fpath))
                except Exception:
                    continue
                if d.get("ban_until_ms", 0) > best["ban_until_ms"]:
                    best["ban_until_ms"] = d["ban_until_ms"]
                dts = d.get("ts", 0.0)
                if _now_sec() - dts <= WEIGHT_TTL_SEC and dts > best.get("ts", 0.0):
                    best["used_weight_1m"] = d.get("used_weight_1m", 0)
                    best["ts"] = dts
        except Exception:
            pass
        return best

    def _read_all_cached(self) -> dict:
        """Чтение с кэшированием на ~1 секунду."""
        now = _now_sec()
        if self._cache is not None and now - self._cache_ts < 1.0:
            return self._cache
        data = self._read_all()
        self._cache = data
        self._cache_ts = now
        return data

    def _write_one(self, path: Path, data: dict) -> None:
        """Запись с дросселем не чаще раза в секунду (используется record_response)."""
        now = _now_sec()
        if now - self._last_write_ts < WRITE_INTERVAL_SEC:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(path) + f".tmp.{os.getpid()}.{int(now * 1000000)}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, str(path))
            self._last_write_ts = now
        except Exception as exc:
            logger.warning(
                f"[RATE_LIMIT] Не удалось записать состояние gate в {path}: {exc}"
            )

    # Обратная совместимость: старые тесты могут патчить gate._write
    _write = _write_one

    def _write_file(self, path: Path, data: dict) -> None:
        """Запись БЕЗ дросселя (используется record_ban)."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = str(path) + f".tmp.{os.getpid()}.{int(_now_ms())}"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            os.replace(tmp, str(path))
        except Exception as exc:
            logger.warning(
                f"[RATE_LIMIT] Не удалось записать бан в {path}: {exc}"
            )

    # ------------------------------------------------------------------ #
    #  Gate                                                              #
    # ------------------------------------------------------------------ #
    def is_banned(self) -> bool:
        self._cache_ts = 0.0
        data = self._read_all_cached()
        return data.get("ban_until_ms", 0) > _now_ms()

    def check_and_reserve(
        self, weight: int = DEFAULT_WEIGHT, is_critical: bool = False
    ) -> tuple[bool, str]:
        """Проверить доступность запроса.

        КРИТИЧНЫЕ запросы проходят всегда (кроме явного бана).
        ВТОРОСТЕПЕННЫЕ пропускаются, если used_weight_1m > shed_at или бан.
        """
        try:
            data = self._read_all_cached()
        except Exception:
            logger.warning("[RATE_LIMIT] gate: не удалось прочитать состояние, пропуск")
            return True, "ok"

        ban_until = data.get("ban_until_ms", 0)
        if ban_until > _now_ms():
            remaining = (ban_until - _now_ms()) / 1000.0
            return False, f"banned until {ban_until} ({remaining:.0f}s left)"

        total_used = data.get("used_weight_1m", 0)
        if not is_critical and total_used / self.weight_limit_1m > self.shed_at:
            self._deferred_count += 1
            self._maybe_log_stats()
            return False, f"shed {total_used}/{self.weight_limit_1m}"

        return True, "ok"

    def record_response(
        self, weight_used: int, ban_until_ms: Optional[int] = None
    ) -> None:
        """Записать результат ответа (с дросселем 1 сек на запись)."""
        now = _now_sec()
        data = {"ban_until_ms": 0, "used_weight_1m": weight_used, "ts": now}
        if ban_until_ms and ban_until_ms > 0:
            data["ban_until_ms"] = ban_until_ms
        try:
            self._write(self._my_path(), data)
            self._cache_ts = 0.0
        except Exception:
            pass

    def record_ban(self, ban_until_ms: int) -> None:
        """Записать бан БЕЗ дросселя (сразу)."""
        now = _now_sec()
        data = {"ban_until_ms": ban_until_ms, "used_weight_1m": 0, "ts": now}
        try:
            self._write_file(self._my_path(), data)
            self._cache_ts = 0.0
        except Exception:
            pass

    def record_attempt(self, ban_until_ms: Optional[int] = None) -> None:
        """Записать факт попытки запроса (в т.ч. неудачной)."""
        now = _now_sec()
        data = {"ban_until_ms": 0, "used_weight_1m": 0, "ts": now}
        if ban_until_ms and ban_until_ms > 0:
            data["ban_until_ms"] = ban_until_ms
        try:
            self._write_one(self._my_path(), data)
        except Exception:
            pass

    def _maybe_log_stats(self) -> None:
        now = _now_sec()
        if now - self._last_stats_ts < STATS_INTERVAL_SEC:
            return
        self._last_stats_ts = now
        try:
            data = self._read_all()
        except Exception:
            return
        logger.warning(
            f"[RATE_LIMIT] used_weight_1m={data.get('used_weight_1m', 0)}/"
            f"{self.weight_limit_1m}, deferred={self._deferred_count}, "
            f"banned_until={data.get('ban_until_ms', 0)}"
        )

    def get_status(self) -> dict:
        self._cache_ts = 0.0
        try:
            data = self._read_all_cached()
        except Exception:
            data = {}
        return {
            "ban_until_ms": data.get("ban_until_ms", 0),
            "used_weight_1m": data.get("used_weight_1m", 0),
            "weight_limit_1m": self.weight_limit_1m,
            "shed_at": self.shed_at,
            "ts": data.get("ts", 0.0),
        }


# --------------------------------------------------------------------------- #
#  Веса запросов                                                            #
# --------------------------------------------------------------------------- #
def _estimate_weight(method: str, uri: str) -> int:
    """Приблизительный вес запроса в единицах Binance weight."""
    u = uri.upper()
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
    if method.upper() in ("POST", "PUT", "DELETE"):
        return 1
    return DEFAULT_WEIGHT


def _is_critical(method: str, uri: str, **kwargs) -> bool:
    """Критические запросы: проходят ВСЕГДА (кроме явного бана), не режутся по весу.

    Критичные:
    - любые POST/PUT/DELETE
    - ордера/отмены (ORDER в URI)
    - позиция /positionRisk
    - аккаунт /account
    - биржа /exchangeInfo
    - маржа /marginType, плечо /leverage, открытые ордера /openOrders
    - цена тикера /ticker/price
    - свечи /klines с limit >= 100 (параметр limit из kwargs, затем из URI)

    НЕ критичные (режутся при превышении веса): любые остальные запросы.
    """
    u = uri.upper()

    if method.upper() in ("POST", "PUT", "DELETE"):
        return True

    markers = ("ORDER", "ALGO", "POSITIONRISK", "/ACCOUNT", "EXCHANGEINFO",
               "LEVERAGE", "OPENORDERS", "LISTENKEY", "MARGINTYPE", "TICKER/PRICE")
    if any(m in u for m in markers):
        return True

    if "/KLINES" in u:
        limit = kwargs.get("limit")
        if limit is None:
            params = kwargs.get("data") or kwargs.get("params") or {}
            if isinstance(params, dict):
                limit = params.get("limit")
        if limit is None:
            match = re.search(r"[?&]limit=(\d+)", u, re.IGNORECASE)
            if match:
                try:
                    limit = int(match.group(1))
                except ValueError:
                    limit = None
        if limit is not None:
            try:
                if int(limit) >= 100:
                    return True
            except (ValueError, TypeError):
                pass

    return False


# --------------------------------------------------------------------------- #
#  Обёртка AsyncClient._request                                             #
# --------------------------------------------------------------------------- #
def _wrap_client(client, gate: AccountGate):
    original_request = client._request

    async def gated_request(method, uri, signed, force_params=False, **kwargs):
        weight = _estimate_weight(method, uri)
        is_critical = _is_critical(method, uri, **kwargs)

        try:
            allowed, reason = gate.check_and_reserve(weight, is_critical=is_critical)
        except Exception:
            logger.warning("[RATE_LIMIT] gate check failed, allowing request")
            allowed = True

        if not allowed:
            raise GateBlocked(reason, code=-1003 if reason.startswith("banned until") else -429)

        try:
            response = await original_request(
                method, uri, signed, force_params, **kwargs
            )
        except Exception as e:
            try:
                resp = getattr(client, "response", None)
                status = getattr(e, "status_code", None)
                ban = _parse_ban_until(str(e))
                if ban is None and status in (418, 429):
                    ra = _header(resp, "Retry-After")
                    ban = _now_ms() + int(float(ra) * 1000 if ra else RETRY_AFTER_DEFAULT_SEC * 1000)
                if ban:
                    gate.record_ban(ban)
            except Exception:
                pass
            raise

        # Записываем фактический вес из заголовка ответа (замена, не сложение)
        # Если заголовка нет — не пишем ничего (не подставляем оценку).
        try:
            headers_obj = getattr(client, "response", None)
            if headers_obj is not None:
                headers_dict = getattr(headers_obj, "headers", headers_obj)
                hw = headers_dict.get("X-MBX-USED-WEIGHT-1M") or headers_dict.get(
                    "x-mbx-used-weight-1m"
                )
                if hw is not None:
                    gate.record_response(int(hw))
        except Exception:
            pass

        return response

    client._request = gated_request
    return client


def wrap_client(client, gate: AccountGate):
    return _wrap_client(client, gate)
