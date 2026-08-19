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
        if not self.cfg.enabled:
            return None

        if self.cfg.mock:
            return True

        if not self._symbol_can_call(symbol, preset):
            logger.debug(f"[LLM] Skip {symbol} {preset}: per-symbol cooldown")
            return None

        await self.limiter.acquire()

        providers = self._build_provider_list()
        for provider in providers:
            if self.breaker.is_blocked(provider.name):
                logger.debug(f"[LLM] Skip blocked provider: {provider.name}")
                continue
            try:
                result = await provider.call(symbol, direction, preset, entry_price, sl_price, tp_price, indicators)
                self.breaker.clear(provider.name)
                self._symbol_mark(symbol, preset)
                return result
            except Exception as e:
                logger.warning(f"[LLM] Provider {provider.name} error: {e}")
                self.breaker.block(provider.name, hard=self._is_hard_error(e))
                await asyncio.sleep(self.cfg.provider_retry_delay_sec)

        return None

    def _build_provider_list(self) -> list["_BaseProvider"]:
        providers: list[_BaseProvider] = []
        if self.cfg.groq_api_key:
            providers.append(_GroqProvider(self.cfg.groq_api_key, self.cfg.groq_model or "groq/compound-mini"))
        if self.cfg.gemini_api_key:
            providers.append(_GeminiProvider(self.cfg.gemini_api_key, self.cfg.gemini_model or "gemini-2.0-flash-exp"))
        if self.cfg.api_key:
            providers.append(_OpenRouterProvider(self.cfg.api_key, self.cfg.model, self.cfg.fallback_list))
        return providers

    @staticmethod
    def _is_hard_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "429" not in msg and "rate limit" not in msg


# ── Providers ─────────────────────────────────────────────────────────────


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

    async def call(self, symbol, direction, preset, entry_price, sl_price, tp_price, indicators) -> bool:
        try:
            from groq import AsyncGroq
        except ImportError:
            logger.warning("[LLM] groq package not installed")
            raise RuntimeError("groq not installed")
        client = AsyncGroq(api_key=self.api_key)
        prompt = self._build_prompt(symbol, direction, preset, entry_price, sl_price, tp_price, indicators)
        resp = await client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": "You are a trading assistant. Reply only with JSON: {\"approve\": true/false, \"confidence\": 0-1, \"reason\": \"...\"}"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=64,
        )
        text = resp.choices[0].message.content or ""
        return self._parse(text)

    def _build_prompt(self, symbol, direction, preset, entry, sl, tp, ind):
        return (
            f"Signal: {direction} {symbol} preset={preset} entry={entry} SL={sl} TP={tp}. "
            f"RSI={ind.get('rsi', 0):.1f} MACD_hist={ind.get('macd_hist', 0):.6f} "
            f"ATR={ind.get('atr', 0):.6f} BB=[{ind.get('bb_lower', 0):.4f}..{ind.get('bb_upper', 0):.4f}]"
        )

    def _parse(self, text: str) -> bool:
        try:
            data = json.loads(text)
            return bool(data.get("approve", False))
        except Exception:
            return False


class _GeminiProvider(_BaseProvider):
    name = "gemini"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    async def call(self, symbol, direction, preset, entry_price, sl_price, tp_price, indicators) -> bool:
        try:
            import google.generativeai as genai
        except ImportError:
            logger.warning("[LLM] google-generativeai package not installed")
            raise RuntimeError("gemini not installed")
        genai.configure(api_key=self.api_key)
        model = genai.GenerativeModel(self.model)
        prompt = self._build_prompt(symbol, direction, preset, entry_price, sl_price, tp_price, indicators)
        resp = await model.generate_content_async(
            f"You are a trading assistant. Reply only with JSON: {{'approve': true/false, 'confidence': 0-1, 'reason': '...'}}\n\n{prompt}"
        )
        text = resp.text or ""
        return self._parse(text)

    def _build_prompt(self, symbol, direction, preset, entry, sl, tp, ind):
        return (
            f"Signal: {direction} {symbol} preset={preset} entry={entry} SL={sl} TP={tp}. "
            f"RSI={ind.get('rsi', 0):.1f} MACD_hist={ind.get('macd_hist', 0):.6f} "
            f"ATR={ind.get('atr', 0):.6f} BB=[{ind.get('bb_lower', 0):.4f}..{ind.get('bb_upper', 0):.4f}]"
        )

    def _parse(self, text: str) -> bool:
        try:
            data = json.loads(text)
            return bool(data.get("approve", False))
        except Exception:
            return False


class _OpenRouterProvider(_BaseProvider):
    name = "openrouter"

    def __init__(self, api_key: str, model: str, fallback_models: list[str]):
        self.api_key = api_key
        self.model = model
        self.fallback_models = fallback_models

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
                        "max_tokens": 64,
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
        return False

    def _build_prompt(self, symbol, direction, preset, entry, sl, tp, ind):
        return (
            f"Signal: {direction} {symbol} preset={preset} entry={entry} SL={sl} TP={tp}. "
            f"RSI={ind.get('rsi', 0):.1f} MACD_hist={ind.get('macd_hist', 0):.6f} "
            f"ATR={ind.get('atr', 0):.6f} BB=[{ind.get('bb_lower', 0):.4f}..{ind.get('bb_upper', 0):.4f}]"
        )

    def _parse(self, text: str) -> bool:
        try:
            data = json.loads(text)
            return bool(data.get("approve", False))
        except Exception:
            return False

    @staticmethod
    def _is_hard_error(exc: Exception) -> bool:
        msg = str(exc).lower()
        return "429" not in msg and "rate limit" not in msg
