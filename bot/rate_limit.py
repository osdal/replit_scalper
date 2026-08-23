"""
Устойчивость к rate-limit Binance: оборачивает Binance API вызовы,
чтобы при ошибках -1003 (IP ban) и -429 (too many requests) бот НЕ падал,
а ждал с backoff и повторял.

- -1003 — IP забанен до определённого времени. Ждём до момента снятия бана + буфер.
- -429  — превышена частота запросов. Ждём с экспоненциальным backoff.
- Прочие сетевые ошибки тоже ретраим несколько раз, чтобы случайные сбои не роняли бота.

Используется в первую очередь для запросов klines (самые частые). Для торговых
опросов в фоне тоже безопасно применять — timeout короткий, а отложить на несколько
секунд при лимите лучше, чем упасть.
"""
import asyncio
import logging
import time

from binance.exceptions import BinanceAPIException, BinanceRequestException

logger = logging.getLogger("rate_limit")

# Коды, при которых ждём снятия бана / освобождения лимита.
_RATE_LIMIT_CODES = {-1003, -429}
# Коды, которые НЕ ретраим (не проблема лимитов) — например -1001 (internal error)
# и т.п. пока просто не ретраим timeout-only; прочие ретраим ограниченно.
_BAN_TIMESTAMP_RE = None


def _parse_ban_until(message: str):
    """Из текста '-1003 ... banned until 1787463454222' достаёт ms-таймстамп снятия бана."""
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


async def with_retry(coro_factory, *, max_retries=5, base_delay=2.0, max_delay=60.0,
                     log=None, retry_codes=None):
    """
    Запускает coro_factory() (возвращающую awaitable запрос к Binance) и в случае
    rate-limit ошибки ждёт с backoff и повторяет, вместо того чтобы бросать дальше.

    Возвращает результат успешного вызова. Если после всех ретраев всё равно упало
    по rate-limit — поднимает последнее исключение.

    coro_factory — функция без аргументов, возвращающая awaitable. Нужна функция,
    а не корутина, чтобы можно было пересоздать awaitable для повторного вызова.
    """
    retry_codes = retry_codes or _RATE_LIMIT_CODES
    attempt = 0
    last_exc = None
    while attempt <= max_retries:
        try:
            return await coro_factory()
        except BinanceAPIException as e:
            code = getattr(e, "code", 0)
            last_exc = e
            if code not in retry_codes:
                raise
            # Вычисляем нужную паузу.
            if code == -1003:
                until = _parse_ban_until(getattr(e, "message", "") or "")
                if until:
                    remaining = (until - int(time.time() * 1000)) / 1000.0
                    delay = max(remaining + 3.0, 1.0)
                    # Не ждать неразумно долго (например, бан на сутки вперёд) —
                    # всё равно ретраим через max_delay, чтобы не висеть навечно.
                    delay = min(delay, max_delay)
                    if log:
                        log.warning(f"[RATE_LIMIT] IP banned -1003, waiting {delay:.0f}s "
                                    f"(until ban lifts) before retry")
                else:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    if log:
                        log.warning(f"[RATE_LIMIT] -1003 (no ban time), backoff {delay:.0f}s")
            else:  # -429
                delay = min(base_delay * (2 ** attempt), max_delay)
                if log:
                    log.warning(f"[RATE_LIMIT] -429 too many requests, backoff {delay:.0f}s")
        except BinanceRequestException as e:
            last_exc = e
            # Сетевая ошибка — тоже ретраим с backoff.
            delay = min(base_delay * (2 ** attempt), max_delay)
            if log:
                log.warning(f"[RATE_LIMIT] request error, backoff {delay:.0f}s: {e}")
        except (asyncio.TimeoutError, TimeoutError) as e:
            last_exc = e
            delay = min(base_delay * (2 ** attempt), max_delay)
            if log:
                log.warning(f"[RATE_LIMIT] timeout, backoff {delay:.0f}s")
        attempt += 1
        if attempt <= max_retries:
            await asyncio.sleep(delay)
    if log:
        log.error(f"[RATE_LIMIT] giving up after {max_retries} retries: {last_exc}")
    raise last_exc
