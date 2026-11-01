"""Provider interfaces, retries with backoff, and a circuit breaker."""

from __future__ import annotations

import asyncio
import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol


class ProviderError(Exception):
    def __init__(self, message: str, *, transient: bool, status: int | None = None):
        super().__init__(message)
        self.transient = transient
        self.status = status


TRANSIENT_STATUS = {408, 409, 425, 429, 500, 502, 503, 504, 529}


@dataclass
class SystemBlock:
    text: str
    cache: bool = False  # stable prefix: ask the provider to cache it


@dataclass
class LLMResult:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    model: str = ""
    provider: str = ""


class LLM(Protocol):
    provider: str
    family: str
    model: str

    async def complete(
        self, system: list[SystemBlock], user: str, *, temperature: float = 0.2, max_tokens: int = 8000,
        json_mode: bool = True,
    ) -> LLMResult: ...


class NMT(Protocol):
    provider: str
    family: str
    model: str

    async def translate(self, texts: list[str], source: str, target: str) -> list[str]: ...


@dataclass
class CircuitBreaker:
    threshold: int = 5
    cooldown: float = 60.0
    failures: int = 0
    opened_at: float = 0.0

    def allow(self) -> bool:
        if self.failures < self.threshold:
            return True
        if time.monotonic() - self.opened_at >= self.cooldown:
            self.failures = self.threshold - 1  # half-open: allow one trial
            return True
        return False

    def success(self) -> None:
        self.failures = 0

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()


@dataclass
class RetryPolicy:
    attempts: int = 6
    base: float = 1.0
    cap: float = 60.0
    sleep: Callable[[float], Awaitable[None]] = field(default=asyncio.sleep)


async def with_retries(fn: Callable[[], Awaitable], policy: RetryPolicy, breaker: CircuitBreaker):
    last: Exception | None = None
    for attempt in range(policy.attempts):
        if not breaker.allow():
            raise ProviderError("Provider circuit is open after repeated failures", transient=True)
        try:
            result = await fn()
            breaker.success()
            return result
        except ProviderError as exc:
            last = exc
            breaker.failure()
            if not exc.transient or attempt == policy.attempts - 1:
                raise
            delay = min(policy.cap, policy.base * 2**attempt) * (0.5 + random.random() / 2)
            await policy.sleep(delay)
    raise last  # type: ignore[misc]


_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json(text: str) -> dict:
    """Parse a model's JSON reply, tolerating code fences and leading prose."""
    s = _FENCE.sub("", text.strip())
    try:
        val = json.loads(s)
    except json.JSONDecodeError:
        start, end = s.find("{"), s.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("The model reply contained no JSON object")
        val = json.loads(s[start : end + 1])
    if not isinstance(val, dict):
        raise ValueError("The model reply JSON is not an object")
    return val
