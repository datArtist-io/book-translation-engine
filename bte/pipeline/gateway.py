"""One door for every model call: retries, circuit breakers, concurrency limits, response
caching keyed by an idempotency hash, and per-job token budgets.

Because every call is cached under a key built from (provider, model, prompt version,
prompt text, temperature, purpose), re-running a crashed job replays finished calls from
the cache instead of paying for them again.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from typing import Protocol

from .. import PROMPT_VERSION
from ..providers.base import CircuitBreaker, LLMResult, ProviderError, RetryPolicy, SystemBlock, parse_json, with_retries


class BudgetExceeded(Exception):
    pass


class ResponseCache(Protocol):
    def get(self, key: str) -> dict | None: ...
    def put(self, key: str, value: dict) -> None: ...


class MemoryCache:
    def __init__(self):
        self._d: dict[str, dict] = {}

    def get(self, key: str) -> dict | None:
        return self._d.get(key)

    def put(self, key: str, value: dict) -> None:
        self._d[key] = value


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    calls: int = 0
    cache_hits: int = 0

    def add(self, r: LLMResult) -> None:
        self.input_tokens += r.input_tokens
        self.output_tokens += r.output_tokens
        self.cached_tokens += r.cached_tokens
        self.calls += 1

    def total(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass
class Gateway:
    cache: ResponseCache = field(default_factory=MemoryCache)
    concurrency: int = 4
    token_budget: int = 20_000_000
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    usage: Usage = field(default_factory=Usage)

    def __post_init__(self):
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._breakers: dict[str, CircuitBreaker] = {}

    def _sem(self, name: str) -> asyncio.Semaphore:
        if name not in self._sems:
            self._sems[name] = asyncio.Semaphore(self.concurrency)
        return self._sems[name]

    def _breaker(self, name: str) -> CircuitBreaker:
        return self._breakers.setdefault(name, CircuitBreaker())

    @staticmethod
    def key(*parts) -> str:
        blob = json.dumps([PROMPT_VERSION, *parts], ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()

    def _check_budget(self) -> None:
        if self.usage.total() >= self.token_budget:
            raise BudgetExceeded(f"Token budget of {self.token_budget:,} reached; job paused.")

    async def llm_json(self, llm, system: list[SystemBlock], user: str, *, purpose: str,
                       temperature: float = 0.2, max_tokens: int = 16000) -> tuple[dict, dict]:
        """Call an LLM and parse its JSON. Returns (parsed, meta)."""
        key = self.key("llm", llm.provider, llm.model, purpose, temperature, [b.text for b in system], user)
        hit = self.cache.get(key)
        if hit is not None:
            self.usage.cache_hits += 1
            return hit["parsed"], {**hit["meta"], "cached": True}
        self._check_budget()

        async def call():
            async with self._sem(llm.provider):
                return await llm.complete(system, user, temperature=temperature, max_tokens=max_tokens)

        result: LLMResult = await with_retries(call, self.retry, self._breaker(llm.provider))
        self.usage.add(result)
        try:
            parsed = parse_json(result.text)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ProviderError(f"{llm.provider} returned unreadable JSON: {exc}", transient=False) from exc
        meta = {"provider": llm.provider, "model": llm.model, "family": llm.family,
                "input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
                "cached_tokens": result.cached_tokens, "cached": False}
        self.cache.put(key, {"parsed": parsed, "meta": meta})
        return parsed, meta

    async def nmt(self, nmt, texts: list[str], source: str, target: str) -> tuple[list[str], dict]:
        key = self.key("nmt", nmt.provider, nmt.model, source, target, texts)
        hit = self.cache.get(key)
        if hit is not None:
            self.usage.cache_hits += 1
            return hit["parsed"]["texts"], {**hit["meta"], "cached": True}

        async def call():
            async with self._sem(nmt.provider):
                return await nmt.translate(texts, source, target)

        out = await with_retries(call, self.retry, self._breaker(nmt.provider))
        meta = {"provider": nmt.provider, "model": nmt.model, "family": nmt.family, "cached": False}
        self.cache.put(key, {"parsed": {"texts": out}, "meta": meta})
        return out, meta
