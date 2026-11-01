"""Real providers over HTTPS: Anthropic (Claude), Google Gemini, Google Cloud Translation.

All three are called with plain REST via httpx so tests can replace the transport and
no vendor SDK upgrade can silently change behaviour. API keys travel in headers, never
in URLs (so they never appear in proxy or server logs).
"""

from __future__ import annotations

import html
import re

import httpx

from .base import TRANSIENT_STATUS, LLMResult, ProviderError, SystemBlock

TIMEOUT = httpx.Timeout(300.0, connect=20.0)


def _raise_for(resp: httpx.Response, provider: str) -> None:
    if resp.status_code < 400:
        return
    # Never echo the request (it holds manuscript text); keep only the vendor's short message.
    detail = ""
    try:
        body = resp.json()
        err = body.get("error", body)
        detail = (err.get("message") if isinstance(err, dict) else str(err))[:300]
    except Exception:
        detail = resp.text[:200]
    raise ProviderError(
        f"{provider} returned HTTP {resp.status_code}: {detail}",
        transient=resp.status_code in TRANSIENT_STATUS,
        status=resp.status_code,
    )


class AnthropicLLM:
    provider = "anthropic"
    family = "anthropic"

    def __init__(self, api_key: str, model: str, *, base_url: str = "https://api.anthropic.com",
                 transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY is not set")
        self.model = model
        self._client = httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT, transport=transport)
        self._headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}

    async def complete(self, system: list[SystemBlock], user: str, *, temperature: float = 0.2,
                       max_tokens: int = 8000, json_mode: bool = True) -> LLMResult:
        blocks = []
        for b in system:
            block: dict = {"type": "text", "text": b.text}
            if b.cache:
                block["cache_control"] = {"type": "ephemeral"}
            blocks.append(block)
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": blocks,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            resp = await self._client.post("/v1/messages", json=payload, headers=self._headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"anthropic network error: {type(exc).__name__}", transient=True) from exc
        _raise_for(resp, "anthropic")
        data = resp.json()
        if data.get("stop_reason") == "max_tokens":
            raise ProviderError("anthropic reply was cut off at the token limit", transient=False)
        text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
        usage = data.get("usage", {})
        return LLMResult(
            text=text,
            input_tokens=usage.get("input_tokens", 0) + usage.get("cache_creation_input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cached_tokens=usage.get("cache_read_input_tokens", 0),
            model=self.model,
            provider=self.provider,
        )


class GeminiLLM:
    provider = "gemini"
    family = "google"

    def __init__(self, api_key: str, model: str, *,
                 base_url: str = "https://generativelanguage.googleapis.com",
                 transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise ValueError("GEMINI_API_KEY is not set")
        self.model = model
        self._client = httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT, transport=transport)
        self._headers = {"x-goog-api-key": api_key, "content-type": "application/json"}

    async def complete(self, system: list[SystemBlock], user: str, *, temperature: float = 0.2,
                       max_tokens: int = 8000, json_mode: bool = True) -> LLMResult:
        config: dict = {"temperature": temperature, "maxOutputTokens": max_tokens}
        if json_mode:
            config["responseMimeType"] = "application/json"
        payload = {
            "systemInstruction": {"parts": [{"text": "\n\n".join(b.text for b in system)}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": config,
        }
        try:
            resp = await self._client.post(f"/v1beta/models/{self.model}:generateContent", json=payload,
                                           headers=self._headers)
        except httpx.HTTPError as exc:
            raise ProviderError(f"gemini network error: {type(exc).__name__}", transient=True) from exc
        _raise_for(resp, "gemini")
        data = resp.json()
        cands = data.get("candidates") or []
        if not cands:
            reason = (data.get("promptFeedback") or {}).get("blockReason", "no candidates")
            raise ProviderError(f"gemini returned no answer ({reason})", transient=False)
        cand = cands[0]
        if cand.get("finishReason") == "MAX_TOKENS":
            raise ProviderError("gemini reply was cut off at the token limit", transient=False)
        if cand.get("finishReason") in {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST"}:
            raise ProviderError(f"gemini declined ({cand.get('finishReason')})", transient=False)
        text = "".join(p.get("text", "") for p in cand.get("content", {}).get("parts", []))
        usage = data.get("usageMetadata", {})
        return LLMResult(
            text=text,
            input_tokens=usage.get("promptTokenCount", 0),
            output_tokens=usage.get("candidatesTokenCount", 0),
            cached_tokens=usage.get("cachedContentTokenCount", 0),
            model=self.model,
            provider=self.provider,
        )


_TAG = re.compile(r"<(/?)([gk])(\d+)>|<x(\d+)/>")
_SPAN = re.compile(r'<span id="(?P<id>[gk]\d+)">|</span>|<img id="(?P<x>x\d+)"\s*/?>')


def tags_to_html(tagged: str) -> str:
    def sub(m: re.Match) -> str:
        if m.group(4) is not None:
            return f'<img id="x{m.group(4)}">'
        if m.group(1):
            return "</span>"
        return f'<span id="{m.group(2)}{m.group(3)}">'

    return _TAG.sub(sub, tagged)


def html_to_tags(text: str) -> str:
    stack: list[str] = []
    out: list[str] = []
    pos = 0
    for m in _SPAN.finditer(text):
        out.append(text[pos : m.start()])
        if m.group("id"):
            stack.append(m.group("id"))
            out.append(f"<{m.group('id')}>")
        elif m.group("x"):
            out.append(f"<{m.group('x')}/>")
        else:
            out.append(f"</{stack.pop()}>" if stack else "")
        pos = m.end()
    out.append(text[pos:])
    joined = "".join(out)
    # Re-escape entities to the tagged-text convention (&amp; &lt; &gt; only).
    parts = re.split(r"(<[gk]\d+>|</[gk]\d+>|<x\d+/>)", joined)
    for i in range(0, len(parts), 2):
        raw = html.unescape(parts[i])
        parts[i] = raw.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return "".join(parts)


class GoogleTranslateNMT:
    """Google Cloud Translation (v2 basic, API-key auth). Used as an independent engine."""

    provider = "google_translate"
    family = "google-nmt"
    model = "nmt"

    def __init__(self, api_key: str, *, base_url: str = "https://translation.googleapis.com",
                 transport: httpx.AsyncBaseTransport | None = None):
        if not api_key:
            raise ValueError("GOOGLE_TRANSLATE_API_KEY is not set")
        self._client = httpx.AsyncClient(base_url=base_url, timeout=TIMEOUT, transport=transport)
        self._headers = {"x-goog-api-key": api_key}

    async def translate(self, texts: list[str], source: str, target: str) -> list[str]:
        out: list[str] = []
        for i in range(0, len(texts), 64):
            batch = texts[i : i + 64]
            payload = {"q": [tags_to_html(t) for t in batch], "source": source.split("-")[0],
                       "target": target, "format": "html"}
            try:
                resp = await self._client.post("/language/translate/v2", json=payload, headers=self._headers)
            except httpx.HTTPError as exc:
                raise ProviderError(f"google_translate network error: {type(exc).__name__}", transient=True) from exc
            _raise_for(resp, "google_translate")
            items = resp.json().get("data", {}).get("translations", [])
            if len(items) != len(batch):
                raise ProviderError("google_translate returned the wrong number of translations", transient=True)
            out.extend(html_to_tags(it.get("translatedText", "")) for it in items)
        return out
