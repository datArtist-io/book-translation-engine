"""Sentence splitting and Gale-Church length-based alignment (language-agnostic).

Gale-Church aligns source and target sentences using only character lengths, so it
works for any language pair without a model. A source sentence aligned to nothing
(a 1-0 bead) is a likely omission; a target sentence aligned to nothing (0-1) is a
likely addition. It is a cheap, deterministic omission detector that complements the
model-based checks, which research shows miss omissions more often than hallucinations.
"""

from __future__ import annotations

import math
import re

_ABBREV = {"mr", "mrs", "ms", "dr", "prof", "st", "jr", "sr", "vs", "etc", "e.g", "i.e", "no", "fig", "vol", "p", "pp"}
_BOUNDARY = re.compile(r"([.!?。！？؟।]+[\"'”’)\]]*)(\s+|$)")


def split_sentences(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    out: list[str] = []
    start = 0
    for m in _BOUNDARY.finditer(text):
        end = m.end(1)
        candidate = text[start:end]
        words = candidate.rstrip(".!?\"')]").split()
        last = words[-1].lower() if words else ""
        if m.group(1).startswith(".") and (last in _ABBREV or (len(last) == 1 and last.isalpha())):
            continue
        if candidate.strip():
            out.append(candidate.strip())
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


def _norm_logsf(x: float) -> float:
    # -log of two-sided tail probability of a standard normal
    p = math.erfc(abs(x) / math.sqrt(2))
    return -math.log(max(p, 1e-300))


PRIORS = {(1, 1): 0.89, (1, 0): 0.0099, (0, 1): 0.0099, (2, 1): 0.089, (1, 2): 0.089, (2, 2): 0.011}
S2 = 6.8


def gale_church(src: list[int], tgt: list[int], ratio: float | None = None) -> list[tuple[int, int]]:
    """Return beads as (n_source, n_target) sentence counts, in order."""
    if ratio is None:
        ratio = (sum(tgt) / sum(src)) if sum(src) else 1.0
    ratio = ratio or 1.0
    n, m = len(src), len(tgt)
    inf = float("inf")
    cost = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int] | None]] = [[None] * (m + 1) for _ in range(n + 1)]
    cost[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            if i == 0 and j == 0:
                continue
            for (di, dj), prior in PRIORS.items():
                if i - di < 0 or j - dj < 0 or cost[i - di][j - dj] == inf:
                    continue
                ls = sum(src[i - di : i])
                lt = sum(tgt[j - dj : j])
                mean = (ls * ratio + lt) / 2 if (ls or lt) else 1
                delta = (lt - ls * ratio) / math.sqrt(max(mean, 1) * S2)
                c = cost[i - di][j - dj] + _norm_logsf(delta) - math.log(prior)
                if c < cost[i][j]:
                    cost[i][j] = c
                    back[i][j] = (di, dj)
    beads: list[tuple[int, int]] = []
    i, j = n, m
    while i > 0 or j > 0:
        step = back[i][j]
        if step is None:
            break
        beads.append(step)
        i, j = i - step[0], j - step[1]
    return list(reversed(beads))


def coverage_issues(source: str, target: str, ratio: float | None = None) -> dict:
    """Detect likely omitted or added sentences between a source and target paragraph."""
    s, t = split_sentences(source), split_sentences(target)
    if len(s) < 2 and len(t) < 2:
        return {"omitted": [], "added": [], "source_sentences": len(s), "target_sentences": len(t)}
    beads = gale_church([len(x) for x in s], [len(x) for x in t], ratio)
    # Typical length ratio of clean 1-1 pairs; merged beads that shrink far below it hide omissions.
    one_to_one = []
    i = j = 0
    for di, dj in beads:
        if di == 1 and dj == 1:
            one_to_one.append(len(t[j]) / max(len(s[i]), 1))
        i += di
        j += dj
    typical = sorted(one_to_one)[len(one_to_one) // 2] if one_to_one else (
        (sum(map(len, t)) / max(sum(map(len, s)), 1)) if ratio is None else ratio)
    omitted, added = [], []
    i = j = 0
    for di, dj in beads:
        ls = sum(len(x) for x in s[i : i + di])
        lt = sum(len(x) for x in t[j : j + dj])
        r = lt / max(ls, 1)
        if di and not dj:
            omitted.extend(s[i : i + di])
        elif dj and not di:
            added.extend(t[j : j + dj])
        elif di > dj and r < 0.7 * typical:
            # e.g. two source sentences carried by one short target sentence
            omitted.append(" / ".join(s[i : i + di]))
        elif dj > di and r > 1.45 * typical:
            added.append(" / ".join(t[j : j + dj]))
        elif di == 1 and dj == 1 and len(one_to_one) >= 3 and r < 0.45 * typical and ls >= 40:
            omitted.append(s[i] + " (translation is much shorter)")
        i += di
        j += dj
    return {"omitted": omitted, "added": added, "source_sentences": len(s), "target_sentences": len(t)}
