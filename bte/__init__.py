"""Book Translation Engine: accuracy-first, format-preserving document translation."""

__version__ = "0.1.0"

# Bump whenever any prompt or verification rule changes. It is part of every
# cache and idempotency key, so a change never silently reuses old results.
PROMPT_VERSION = "2026-11-01.1"
