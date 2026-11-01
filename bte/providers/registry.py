"""Build providers from "provider:model" specs in settings."""

from __future__ import annotations

from ..config import Settings
from .mock import MockLLM, MockNMT
from .remote import AnthropicLLM, GeminiLLM, GoogleTranslateNMT


def build_llm(spec: str, settings: Settings):
    spec = (spec or "").strip()
    if not spec:
        return None
    provider, _, model = spec.partition(":")
    provider = provider.lower()
    if provider == "mock":
        return MockLLM(family=f"mock-{model or 'a'}", model=model or "mock-1")
    if provider == "anthropic":
        return AnthropicLLM(settings.anthropic_api_key, model or "claude-opus-5-5")
    if provider == "gemini":
        return GeminiLLM(settings.gemini_api_key, model or "gemini-2.5-pro")
    raise ValueError(f"Unknown LLM provider in {spec!r}")


def build_nmt(spec: str, settings: Settings):
    spec = (spec or "").strip().lower()
    if not spec or spec == "none":
        return None
    if spec == "mock":
        return MockNMT()
    if spec == "google_translate":
        if not settings.google_translate_api_key:
            return None
        return GoogleTranslateNMT(settings.google_translate_api_key)
    raise ValueError(f"Unknown NMT provider {spec!r}")
