"""Statistical release: a book ships only when a random sample proves its error rate.

Same idea as acceptance sampling in manufacturing (ISO 2859-1): review a random sample,
and compute a one-sided upper confidence bound on the true error rate. With zero errors
in n sentences, the 95% bound is about 3/n (the "rule of three"); 299 clean sentences
support "fewer than 1 major error per 100 sentences at 95% confidence".
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


def _binom_cdf(k: int, n: int, p: float) -> float:
    if p <= 0:
        return 1.0
    if p >= 1:
        return 1.0 if k >= n else 0.0
    total = 0.0
    log_p, log_q = math.log(p), math.log1p(-p)
    for i in range(0, k + 1):
        total += math.exp(math.lgamma(n + 1) - math.lgamma(i + 1) - math.lgamma(n - i + 1) + i * log_p + (n - i) * log_q)
    return min(1.0, total)


def upper_bound(errors: int, n: int, confidence: float = 0.95) -> float:
    """Exact (Clopper-Pearson) one-sided upper bound on an error rate."""
    if n <= 0:
        return 1.0
    if errors >= n:
        return 1.0
    alpha = 1 - confidence
    if errors == 0:
        return 1 - alpha ** (1 / n)
    lo, hi = errors / n, 1.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if _binom_cdf(errors, n, mid) > alpha:
            lo = mid
        else:
            hi = mid
    return hi


def required_sentences(target_rate: float, confidence: float = 0.95) -> int:
    """Sentences to review with zero errors to prove the rate is below target."""
    return math.ceil(math.log(1 - confidence) / math.log(1 - target_rate))


@dataclass
class SampleItem:
    chunk_id: int
    chapter: int
    sentences: int


def draw_sample(items: list[SampleItem], needed: int, seed: int) -> list[int]:
    """Stratified random sample: round-robin across chapters until enough sentences."""
    rng = random.Random(seed)
    by_chapter: dict[int, list[SampleItem]] = {}
    for it in items:
        by_chapter.setdefault(it.chapter, []).append(it)
    for lst in by_chapter.values():
        rng.shuffle(lst)
    chosen: list[int] = []
    total = 0
    chapters = sorted(by_chapter)
    while total < needed and any(by_chapter.values()):
        for ch in chapters:
            if by_chapter[ch] and total < needed:
                it = by_chapter[ch].pop()
                chosen.append(it.chunk_id)
                total += max(it.sentences, 1)
    return chosen


@dataclass
class ReleaseDecision:
    passed: bool
    sentences: int
    major: int
    critical: int
    bound: float
    target: float
    message: str


def decide(sentences: int, major: int, critical: int, target: float, confidence: float = 0.95) -> ReleaseDecision:
    bound = upper_bound(major + critical, sentences, confidence)
    if critical:
        return ReleaseDecision(False, sentences, major, critical, bound, target,
                               f"{critical} critical error(s) found in the sample.")
    if bound <= target:
        return ReleaseDecision(True, sentences, major, critical, bound, target,
                               f"Major-error rate below {target:.1%} per sentence at {confidence:.0%} confidence "
                               f"(upper bound {bound:.2%}, {sentences} sentences reviewed).")
    return ReleaseDecision(False, sentences, major, critical, bound, target,
                           f"Upper bound {bound:.2%} exceeds the {target:.1%} limit "
                           f"({major} major error(s) in {sentences} sentences).")
