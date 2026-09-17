"""
Устойчивость к rate-limit Binance: оборачивает Binance API вызовы,
чтобы при ошибках -1003 (IP ban) и -429 (too many requests) бот НЕ падал,
а ждал с backoff и повторял.

- -1003 — IP забанен до определённого времени. Ждём до момента снятия бана + буфер.
  После ожидания запрос НЕ повторяется в этом же вызове: caller пропускает цикл.
- -429  — превышена частота запросов. Ждём с экспоненциальным backoff.
- Прочие сетевые ошибки тоже ретраим несколько раз, чтобы случайные сбои не роняли бота.

Используется в первую очередь для запросов klines (самые частые). Для торговых
опросов в фоне тоже безопасно применять — timeout короткий, а отложить на несколько
секунд при лимите лучше, чем упасть.
"""
import asyncio
import logging
import random
import time

from binance.exceptions import BinanceAPIException, BinanceRequestException

logger = logging.getLogger("rate_limit")

# Коды, при которых ждём снятия бана / освобождения лимита.
_RATE_LIMIT_CODES = {-1003, -429}

# --- Ban-aware backoff --------------------------------------------------------
# -1003 (IP ban): ждём до таймстампа снятия бана + небольшой джиттер.
BAN_JITTER_MIN_SEC = 1.0
BAN_JITTER_MAX_SEC = 3.0
# Максимальная добавочная пауза за серию повторных банов (5 мин).
BAN_MAX_EXTRA_SEC = 300.0
# Повторные баны в пределах этого окна считаются серией и эскалируют паузу.
BAN_WINDOW_SEC = 300.0
# Предел показателя экспоненты (2 ** 6 = 64).
BAN_MAX_ESCALATION = 6

# Состояние серии банов. asyncio однопоточный — отдельный lock не нужен.
_ban_state = {"count": 0, "last_ts": 0.0}
# Wall-clock время снятия текущего (уже обрабатываемого) бана. Позволяет не
# эскалировать паузу повторно для одного и того же бана, когда его ловят
# несколько запросов подряд.
_active_ban_until_wall = 0.0


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


def is_ban_error(exc) -> bool:
    """True для BinanceAPIException с кодом -1003 (IP ban)."""
    return isinstance(exc, BinanceAPIException) and getattr(exc, "code", 0) == -1003


def is_rate_limit_error(exc) -> bool:
    """True для -1003 / -429."""
    return isinstance(exc, BinanceAPIException) and getattr(exc, "code", 0) in _RATE_LIMIT_CODES


def reset_ban_state() -> None:
    """Сбрасывает счётчик серии банов после успешного запроса."""
    global _active_ban_until_wall
    _ban_state["count"] = 0
    _ban_state["last_ts"] = 0.0
    _active_ban_until_wall = 0.0


def _next_ban_count(now: float) -> int:
    """Счётчик банов в пределах BAN_WINDOW_SEC (серия). Вне окна — сбрасывается."""
    st = _ban_state
    if now - st["last_ts"] > BAN_WINDOW_SEC:
        st["count"] = 0
    st["count"] += 1
    st["last_ts"] = now
    return st["count"]


def ban_wait_seconds(exc, now: float = None) -> float:
    """
    Пауза при -1003:

        wait = max(0, ban_until - now)                       # остаток до снятия
             + min(jitter * 2**(n-1), BAN_MAX_EXTRA_SEC)     # джиттер 1..3с, эскалация

    где n — номер бана в серии (окно BAN_WINDOW_SEC), эскалация ограничена
    BAN_MAX_EXTRA_SEC (5 мин). Если таймстамп снятия не распознан — возвращаем
    только джиттер-часть.
    """
    now = time.time() if now is None else now
    count = _next_ban_count(now)
    jitter = random.uniform(BAN_JITTER_MIN_SEC, BAN_JITTER_MAX_SEC)
    escalation = 2 ** min(count - 1, BAN_MAX_ESCALATION)
    extra = min(jitter * escalation, BAN_MAX_EXTRA_SEC)
    until = _parse_ban_until(getattr(exc, "message", "") or "")
    if until:
        remaining = max(0.0, (until - int(now * 1000)) / 1000.0)
        return remaining + extra
    return extra


async def wait_for_ban(exc, log=None) -> bool:
    """
    Если exc — бан (-1003), спит до снятия бана (+backoff) и возвращает True.
    НЕ повторяет запрос: caller должен пропустить текущий цикл.

    Для уже обрабатываемого бана (тот же/более ранний ban_until) пауза повторно
    НЕ эскалируется — ждём только остаток. Для не-бановых ошибок возвращает
    False, ничего не делая.
    """
    global _active_ban_until_wall
    if not is_ban_error(exc):
        return False

    now = time.time()
    until_ms = _parse_ban_until(getattr(exc, "message", "") or "")
    ban_until_wall = (until_ms / 1000.0) if until_ms else 0.0

    if ban_until_wall > now and ban_until_wall <= _active_ban_until_wall:
        remaining = max(0.0, _active_ban_until_wall - now)
        if log and remaining > 0:
            log.warning(
                f"[RATE_LIMIT] IP banned -1003 (same ban), waiting {remaining:.0f}s "
                f"before next cycle"
            )
        if remaining > 0:
            await asyncio.sleep(remaining)
        return True

    delay = ban_wait_seconds(exc, now)
    if ban_until_wall > 0:
        _active_ban_until_wall = max(ban_until_wall, now + delay)
    if log:
        log.warning(
            f"[RATE_LIMIT] IP banned -1003, waiting {delay:.0f}s "
            f"(ban lift + backoff, consecutive={_ban_state['count']}) before next cycle"
        )
    await asyncio.sleep(delay)
    return True


async def with_retry(coro_factory, *, max_retries=5, base_delay=2.0, max_delay=60.0,
                     log=None, retry_codes=None):
    """
    Запускает coro_factory() (возвращающую awaitable запрос к Binance) и в случае
    rate-limit ошибки ждёт с backoff и повторяет, вместо того чтобы бросать дальше.

    Возвращает результат успешного вызова.

    При -1003 (IP ban) НЕ повторяет в этом же вызове: ждёт снятия бана и
    поднимает исключение, чтобы caller пропустил текущий цикл. При -429 и
    сетевых ошибках ретраит с backoff, как раньше.

    coro_factory — функция без аргументов, возвращающая awaitable. Нужна функция,
    а не корутина, чтобы можно было пересоздать awaitable для повторного вызова.
    """
    retry_codes = retry_codes or _RATE_LIMIT_CODES
    attempt = 0
    last_exc = None
    while attempt <= max_retries:
        try:
            result = await coro_factory()
            # Успешный запрос сбрасывает серию банов.
            reset_ban_state()
            return result
        except BinanceAPIException as e:
            code = getattr(e, "code", 0)
            last_exc = e
            if code not in retry_codes:
                raise
            if code == -1003:
                # Ждём снятия бана и НЕ повторяем здесь — caller пропускает цикл,
                # чтобы не долбить API во время бана.
                await wait_for_ban(e, log=log)
                raise
            # -429 — короткий backoff и повтор в рамках вызова.
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
