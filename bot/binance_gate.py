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
import time
from pathlib import Path
from typing import Optional

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


def _parse_retry_after(message: Optional[str], headers: Optional[dict] = None) -> Optional[float]:
    if headers is not None:
        try:
            ra = headers.get("Retry-After") or headers.get("retry-after")
            if ra is not None:
                return int(ra)
        except (ValueError, TypeError):
            pass
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
        return int(digits) / 1000.0
    except ValueError:
        return None


class GateBlocked(Exception):
    def __init__(self, message: str, code: int = -429):
        super().__init__(message)
        self.code = code


class AccountGate:
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

    def _my_path(self) -> Path:
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

    def _write_one(self, path: Path, data: dict) -> None:
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

    # Backwards-compatible alias for _write_one
    _write = _write_one

    def is_banned(self) -> bool:
        data = self._read_all()
        return data.get("ban_until_ms", 0) > _now_ms()

    def check_and_reserve(
        self, weight: int = DEFAULT_WEIGHT, is_critical: bool = False
    ) -> tuple[bool, str]:
        try:
            data = self._read_all()
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
        now = _now_sec()
        data = {"ban_until_ms": 0, "used_weight_1m": weight_used, "ts": now}
        if ban_until_ms and ban_until_ms > 0:
            data["ban_until_ms"] = ban_until_ms
        try:
            self._write_one(self._my_path(), data)
        except Exception:
            pass

    def record_ban(self, ban_until_ms: int) -> None:
        now = _now_sec()
        data = {"ban_until_ms": ban_until_ms, "used_weight_1m": 0, "ts": now}
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
        try:
            data = self._read_all()
        except Exception:
            data = {}
        return {
            "ban_until_ms": data.get("ban_until_ms", 0),
            "used_weight_1m": data.get("used_weight_1m", 0),
            "weight_limit_1m": self.weight_limit_1m,
            "shed_at": self.shed_at,
            "ts": data.get("ts", 0.0),
        }


def _estimate_weight(method: str, uri: str) -> int:
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


def _is_critical(method: str, uri: str) -> bool:
    u = uri.upper()
    if method.upper() in ("POST", "DELETE", "PUT"):
        if any(k in u for k in ("ORDER", "ALGOORDER")):
            return True
    if "/POSITIONRISK" in u or "/ACCOUNT" in u:
        return True
    if "/EXCHANGEINFO" in u:
        return True
    if "/LEVERAGEBRACKET" in u:
        return True
    if method.upper() == "POST" and "/LEVERAGE" in u:
        return True
    if "/OPENORDERS" in u:
        return True
    if "/KLINES" in u and "LIMIT=100" in u:
        return True
    return False


def _wrap_client(client, gate: AccountGate):
    original_request = client._request

    async def gated_request(method, uri, signed, force_params=False, **kwargs):
        weight = _estimate_weight(method, uri)
        is_critical = _is_critical(method, uri)

        try:
            allowed, reason = gate.check_and_reserve(weight, is_critical=is_critical)
        except Exception:
            logger.warning("[RATE_LIMIT] gate check failed, allowing request")
            allowed = True

        if not allowed:
            e = Exception(f"gate shed: {reason} (code -429)")
            e.code = -429
            raise e

        try:
            response = await original_request(
                method, uri, signed, force_params, **kwargs
            )
        except Exception as e:
            code = getattr(e, "code", 0)
            if code == -1003:
                ban_until = _parse_ban_until(str(e))
                if ban_until:
                    try:
                        gate.record_ban(ban_until)
                    except Exception:
                        pass
            elif code == -429:
                headers = getattr(client, "response", None)
                ra_seconds = None
                if headers is not None:
                    try:
                        ra = headers.get("Retry-After") or headers.get("retry-after")
                        if ra is not None:
                            ra_seconds = int(ra)
                    except (ValueError, TypeError):
                        pass
                if ra_seconds is None:
                    ra_seconds = RETRY_AFTER_DEFAULT_SEC
                try:
                    gate.record_ban(int(_now_ms() + ra_seconds * 1000))
                except Exception:
                    pass
            raise

        try:
            headers_obj = getattr(client, "response", None)
            used_weight = weight
            if headers_obj is not None:
                try:
                    # client.response is a response object (not a dict),
                    # get .headers from it
                    headers_dict = getattr(headers_obj, "headers", headers_obj)
                    hw = headers_dict.get("X-MBX-USED-WEIGHT-1M") or headers_dict.get(
                        "x-mbx-used-weight-1m"
                    )
                    if hw is not None:
                        used_weight = int(hw)
                except (ValueError, TypeError, AttributeError):
                    pass
        except Exception:
            pass
        gate.record_response(used_weight)

        return response

    client._request = gated_request
    return client


def wrap_client(client, gate: AccountGate):
    return _wrap_client(client, gate)
