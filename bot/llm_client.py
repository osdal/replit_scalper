"""
Optional LLM filter for trading signals.
Providers: Groq, Gemini, OpenRouter.
Circuit breaker: skip provider after N consecutive failures / 429s.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("llm")

@dataclass
class ProviderStatus:
    name: str = ""
    state: str = "idle"          # idle | ok | degraded | blocked | error
    last_error: str = ""
    last_ok_ts: float = 0.0
    errors_since_ok: int = 0


@dataclass
class LLMStatus:
    enabled: bool = False
    last_result: str = "idle"    # idle | approved | rejected | skipped | all_failed
    last_error: str = ""
    last_check_ts: float = 0.0
    providers: dict[str, ProviderStatus] = None

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "last_result": self.last_result,
            "last_error": self.last_error,
            "last_check_ts": self.last_check_ts,
            "providers": {k: v.__dict__ for k, v in (self.providers or {}).items()},
        }


# ── Config ────────────────────────────────────────────────────────────────


@dataclass
class LLMConfig:
    enabled: bool = False
    mock: bool = False
    api_key: str = ""
    model: str = "llama-3.1-70b-versatile"
    fallback_models: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-2.0-flash-exp"
    groq_api_key: str = ""
    groq_model: str = "groq/compound-mini"
    confidence_threshold: float = 0.7
    calls_per_min: int = 20
    per_symbol_cooldown_min: int = 5
    backoff_sec: float = 60.0
    short_backoff_sec: float = 5.0
    provider_retry_delay_sec: float = 1.0

    @property
    def fallback_list(self) -> list[str]:
        if not self.fallback_models:
            return []
        return [m.strip() for m in self.fallback_models.split(",") if m.strip()]


# ── Circuit breaker ───────────────────────────────────────────────────────


class _CircuitBreaker:
    def __init__(self, backoff_sec: float, short_backoff_sec: float):
        self.backoff_sec = backoff_sec
        self.short_backoff_sec = short_backoff_sec
        self._until: dict[str, float] = {}
        self._short_until: dict[str, float] = {}

    def is_blocked(self, provider: str) -> bool:
        now = time.monotonic()
        u = self._until.get(provider, 0.0)
        su = self._short_until.get(provider, 0.0)
        if now < su:
            return True
        if now < u:
            return True
        return False

    def block(self, provider: str, hard: bool = True) -> None:
        now = time.monotonic()
        if hard:
            self._until[provider] = now + self.backoff_sec
        self._short_until[provider] = now + self.short_backoff_sec

    def clear(self, provider: str) -> None:
        self._until.pop(provider, None)
        self._short_until.pop(provider, None)


# ── Rate limiter ──────────────────────────────────────────────────────────


class _RateLimiter:
    def __init__(self, calls_per_min: int):
        self.calls_per_min = calls_per_min
        self._timestamps: list[float] = []

    async def acquire(self) -> None:
        if self.calls_per_min <= 0:
            return
        now = time.monotonic()
        cutoff = now - 60.0
        self._timestamps = [t for t in self._timestamps if t > cutoff]
        if len(self._timestamps) >= self.calls_per_min:
            wait = self._timestamps[0] + 60.0 - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._timestamps = [t for t in self._timestamps if t > time.monotonic() - 60.0]
        self._timestamps.append(time.monotonic())


# ── LLM Client ────────────────────────────────────────────────────────────


class LLMClient:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg
        self.breaker = _CircuitBreaker(cfg.backoff_sec, cfg.short_backoff_sec)
        self.limiter = _RateLimiter(cfg.calls_per_min)
        self._symbol_cooldowns: dict[str, float] = {}
        self.status = LLMStatus(enabled=cfg.enabled, providers={})

    def _provider_status(self, name: str) -> ProviderStatus:
        ps = self.status.providers.setdefault(name, ProviderStatus(name=name))
        return ps

    def _mark_provider_ok(self, name: str) -> None:
        ps = self._provider_status(name)
        ps.state = "ok"
        ps.last_error = ""
        ps.errors_since_ok = 0
        ps.last_ok_ts = time.time()

    def _mark_provider_error(self, name: str, error: str, hard: bool = True, rate_limit: bool = False) -> None:
        ps = self._provider_status(name)
        ps.errors_since_ok += 1
        ps.last_error = error[:200]
        if rate_limit:
            # 429 — временный лимит: помечаем "degraded" (жёлтый), не "blocked" (красный)
            ps.state = "degraded"
        elif self.breaker.is_blocked(name):
            ps.state = "blocked"
        elif ps.errors_since_ok >= 2:
            ps.state = "degraded"
        else:
            ps.state = "error"

    def _is_symbol_cooldown_active(self, symbol: str, preset: str) -> bool:
        return not self._symbol_can_call(symbol, preset)

    def _symbol_key(self, symbol: str, preset: str) -> str:
        raw = f"{symbol}:{preset}:{int(time.time() / (self.cfg.per_symbol_cooldown_min * 60))}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _symbol_can_call(self, symbol: str, preset: str) -> bool:
        key = self._symbol_key(symbol, preset)
        now = time.monotonic()
        last = self._symbol_cooldowns.get(key, 0.0)
        if now - last < self.cfg.per_symbol_cooldown_min * 60:
            return False
        return True

    def _symbol_mark(self, symbol: str, preset: str) -> None:
        key = self._symbol_key(symbol, preset)
        self._symbol_cooldowns[key] = time.monotonic()

    async def validate(
        self,
        symbol: str,
        direction: str,
        preset: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        indicators: dict,
    ) -> Optional[bool]:
        """Returns True (approve), False (reject), or None (skip/error)."""
        self.status.last_check_ts = time.time()
        if not self.cfg.enabled:
            self.status.last_result = "idle"
            return None

        if self.cfg.mock:
            self.status.last_result = "approved"
            return True

        if not self._symbol_can_call(symbol, preset):
            logger.debug(f"[LLM] Skip {symbol} {preset}: per-symbol cooldown")
            self.status.last_result = "skipped"
            return None

        await self.limiter.acquire()

        providers = self._build_provider_list(symbol)
        rate_limited = []
        for provider in providers:
            name = provider.name
            self._provider_status(name)
            if self.breaker.is_blocked(name):
                ps = self._provider_status(name)
                ps.state = "blocked"
                logger.debug(f"[LLM] Skip blocked provider: {name}")
                continue
            try:
                result = await provider.call(symbol, direction, preset, entry_price, sl_price, tp_price, indicators)
                self.breaker.clear(name)
                self._mark_provider_ok(name)
                self._symbol_mark(symbol, preset)
                self.status.last_error = ""
                if result is False:
                    self.status.last_result = "rejected"
                elif result is True:
                    self.status.last_result = "approved"
                else:
                    self.status.last_result = "skipped"
                return result
            except Exception as e:
                logger.warning(f"[LLM] Provider {name} error: {e}")
                hard = self._is_hard_error(e)
                is_rate_limit = not hard  # 429 / rate limit
                self.breaker.block(name, hard=hard)
                self._mark_provider_error(name, str(e), hard=hard, rate_limit=is_rate_limit)
                if is_rate_limit:
                    rate_limited.append(name)
                await asyncio.sleep(self.cfg.provider_retry_delay_sec)

        if rate_limited:
            # Все провайдеры временно залимичены (429) — не считаем это падением,
            # сигнал просто пропускается без ИИ-проверки.
            self.status.last_result = "rate_limited"
            self.status.last_error = f"Rate limited: {', '.join(rate_limited)}"
        else:
            self.status.last_result = "all_failed"
            self.status.last_error = "All providers failed"
        return None

    def _build_provider_list(self, symbol: str = "") -> list["_BaseProvider"]:
        providers: list[_BaseProvider] = []
        if self.cfg.groq_api_key:
            providers.append(_GroqProvider(self.cfg.groq_api_key, self.cfg.groq_model or "groq/compound-mini"))
        if self.cfg.gemini_api_key:
            providers.append(_GeminiProvider(self.cfg.gemini_api_key, self.cfg.gemini_model or "gemini-2.0-flash-exp"))
        if self.cfg.api_key:
            providers.append(_OpenRouterProvider(self.cfg.api_key, self.cfg.model, self.cfg.fallback_list, breaker=self.breaker))
        # Ротация приоритета по символу: разные боты начинают с разных провайдеров,
        # чтобы в момент закрытия свечи (когда сигналы идут пачкой) 35 ботов не
        # били одновременно в один и тот же ключ Groq.
        if symbol and len(providers) > 1:
            offset = int(hashlib.md5(symbol.encode()).hexdigest(), 16) % len(providers)
            providers = providers[offset:] + providers[:offset]
        return providers

    @staticmethod
    def _is_hard_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "429" not in msg and "rate limit" not in msg


# ── Providers ─────────────────────────────────────────────────────────────


def _build_analysis_prompt(symbol: str, direction: str, preset: str,
                           entry: float, sl: float, tp: float, ind: dict) -> str:
    """Единый промпт анализа сигнала для всех провайдеров.

    Модель получает индикаторы и явные критерии согласия/несогласия с
    направлением сделки, чтобы вердикт был осмысленным, а не случайным.
    """
    rsi = ind.get("rsi", 0)
    macd_hist = ind.get("macd_hist", 0)
    atr = ind.get("atr", 0)
    bb_low = ind.get("bb_lower", 0)
    bb_mid = ind.get("bb_middle", 0)
    bb_up = ind.get("bb_upper", 0)
    vol = ind.get("volume", 0)
    vol_ma = ind.get("volume_ma", 0)
    ema_fast = ind.get("ema_fast", 0)
    ema_slow = ind.get("ema_slow", 0)

    bullish = (
        f"EMA fast {ema_fast:.2f} > slow {ema_slow:.2f}" if ema_fast > ema_slow else
        f"EMA fast {ema_fast:.2f} < slow {ema_slow:.2f}"
    )
    macd_note = "positive (bullish)" if macd_hist > 0 else "negative (bearish)"
    vol_note = f"volume {vol:.0f} > MA {vol_ma:.0f} (impulse)" if vol > vol_ma else f"volume {vol:.0f} <= MA {vol_ma:.0f} (weak)"

    criteria = [
        f"- {direction} {symbol} preset={preset}",
        f"- Entry {entry:.4f} | SL {sl:.4f} (risk {(entry-sl)/entry*100:.2f}%) | TP {tp:.4f} (reward {(tp-entry)/entry*100:.2f}%) | RR {(tp-entry)/abs(entry-sl):.2f}",
        f"- RSI {rsi:.1f} (30-70 neutral; >70 overbought for LONG, <30 oversold for SHORT)",
        f"- MACD histogram {macd_hist:.6f} ({macd_note})",
        f"- Bollinger lower {bb_low:.4f} mid {bb_mid:.4f} upper {bb_up:.4f}",
        f"- {bullish}",
        f"- {vol_note}",
        f"- ATR {atr:.4f} (volatility)",
    ]
    rules = (
        "Rules: approve the signal only if technicals agree with the direction. "
        "For LONG: RSI<70, MACD hist positive, price above mid-BB, EMA fast>slow, volume above MA. "
        "For SHORT: RSI>30, MACD hist negative, price below mid-BB, EMA fast<slow. "
        "Reject if the signal contradicts the trend or RSI is at an extreme against the trade."
    )
    return "\n".join(criteria) + "\n" + rules + '\nReturn JSON {"approve": true/false, "confidence": 0-1, "reason": "brief"}'



class _BaseProvider:
    name: str = "base"

    async def call(
        self,
        symbol: str,
        direction: str,
        preset: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        indicators: dict,
    ) -> bool:
        raise NotImplementedError


class _GroqProvider(_BaseProvider):
    name = "groq"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    async def call(self, symbol, direction, preset, entry_price, sl_price, tp_price, indicators) -> Optional[bool]:
        try:
            import aiohttp
        except ImportError:
            logger.warning("[LLM] aiohttp package not installed")
            raise RuntimeError("aiohttp not installed")
        prompt = self._build_prompt(symbol, direction, preset, entry_price, sl_price, tp_price, indicators)
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a trading assistant. Reply ONLY with a valid JSON object, no other text."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "max_tokens": 100,
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 429:
                    raise RuntimeError("groq rate limit 429")
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    raise RuntimeError(f"groq HTTP {resp.status}: {body}")
                data = await resp.json()
        try:
            text = data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError):
            text = ""
        return self._parse(text)

    def _build_prompt(self, symbol, direction, preset, entry, sl, tp, ind):
        return _build_analysis_prompt(symbol, direction, preset, entry, sl, tp, ind)

    def _parse(self, text: str) -> Optional[bool]:
        try:
            data = json.loads(text)
            val = data.get("approve")
            if val is None:
                return None
            return bool(val)
        except Exception:
            return None


class _GeminiProvider(_BaseProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    async def call(self, symbol, direction, preset, entry_price, sl_price, tp_price, indicators) -> Optional[bool]:
        try:
            import aiohttp
        except ImportError:
            logger.warning("[LLM] aiohttp package not installed")
            raise RuntimeError("aiohttp not installed")
        prompt = self._build_prompt(symbol, direction, preset, entry_price, sl_price, tp_price, indicators)
        # Новые ключи Gemini (AI Studio) работают только через `?key=` в query,
        # Bearer-заголовок возвращает 401.
        # Модель gemini-flash-lite-latest: быстрая, без тяжёлого thinking-блока,
        # который у gemini-2.5-flash съедал выходные токены (ответ обрывался MAX_TOKENS).
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": 200,
                "responseMimeType": "application/json",
            },
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                params={"key": self.api_key},
                json=payload,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status == 429:
                    raise RuntimeError("gemini rate limit 429")
                if resp.status >= 400:
                    body = (await resp.text())[:200]
                    raise RuntimeError(f"gemini HTTP {resp.status}: {body}")
                data = await resp.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError, TypeError):
            text = ""
        return self._parse(text)

    def _build_prompt(self, symbol, direction, preset, entry, sl, tp, ind):
        return _build_analysis_prompt(symbol, direction, preset, entry, sl, tp, ind)

    def _parse(self, text: str) -> Optional[bool]:
        try:
            data = json.loads(text)
            val = data.get("approve")
            if val is None:
                return None
            return bool(val)
        except Exception:
            return None


class _OpenRouterProvider(_BaseProvider):
    name = "openrouter"

    def __init__(self, api_key: str, model: str, fallback_models: list[str], breaker=None):
        self.api_key = api_key
        self.model = model
        self.fallback_models = fallback_models
        self.breaker = breaker if breaker is not None else _CircuitBreaker(60.0, 5.0)

    async def call(self, symbol, direction, preset, entry_price, sl_price, tp_price, indicators) -> bool:
        try:
            import aiohttp
        except ImportError:
            logger.warning("[LLM] aiohttp package not installed")
            raise RuntimeError("aiohttp not installed")
        models = [self.model] + self.fallback_models
        prompt = self._build_prompt(symbol, direction, preset, entry_price, sl_price, tp_price, indicators)
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/replit-scalper",
        }
        async with aiohttp.ClientSession() as session:
            for model in models:
                if self.breaker.is_blocked(f"openrouter:{model}"):
                    continue
                try:
                    payload = {
                        "model": model,
                        "messages": [
                            {"role": "system", "content": "You are a trading assistant. Reply only with JSON: {\"approve\": true/false, \"confidence\": 0-1, \"reason\": \"...\"}"},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": 0.1,
                        "max_tokens": 100,
                    }
                    async with session.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=aiohttp.ClientTimeout(total=15),
                    ) as resp:
                        if resp.status == 429:
                            self.breaker.block(f"openrouter:{model}", hard=False)
                            continue
                        if resp.status >= 400:
                            self.breaker.block(f"openrouter:{model}", hard=True)
                            continue
                        data = await resp.json()
                        text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                        if text:
                            self.breaker.clear(f"openrouter:{model}")
                            return self._parse(text)
                except Exception as e:
                    logger.warning(f"[LLM] OpenRouter model {model} error: {e}")
                    self.breaker.block(f"openrouter:{model}", hard=self._is_hard_error(e))
        return None

    def _build_prompt(self, symbol, direction, preset, entry, sl, tp, ind):
        return _build_analysis_prompt(symbol, direction, preset, entry, sl, tp, ind)

    def _parse(self, text: str) -> Optional[bool]:
        try:
            data = json.loads(text)
            val = data.get("approve")
            if val is None:
                return None
            return bool(val)
        except Exception:
            return None

    @staticmethod
    def _is_hard_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "429" not in msg and "rate limit" not in msg
