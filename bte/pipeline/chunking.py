"""Group paragraphs into chapters and translation chunks.

Chunks are a few paragraphs (default up to ~4,000 characters, about 1,000 tokens): big
enough for document-level context, small enough to avoid the omissions that grow with
window size. Chunks never cross a heading or a part (body, footnotes, headers).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from ..docx.segments import Segment

NOTES_CHAPTER = 10_000  # footnotes, endnotes, headers and footers


@dataclass
class ChunkPlan:
    chapter: int
    order: int
    sids: list[str]
    source_hash: str


def assign_chapters(segments: list[Segment]) -> dict[str, int]:
    chapters: dict[str, int] = {}
    current = 0
    seen_text = False
    for seg in segments:
        if seg.part != "word/document.xml":
            chapters[seg.sid] = NOTES_CHAPTER
            continue
        if seg.heading_level in (0, 1) and seg.translatable and seen_text:
            current += 1
        if seg.translatable:
            seen_text = True
        chapters[seg.sid] = current
    return chapters


def plan_chunks(segments: list[Segment], chapters: dict[str, int], max_chars: int = 4000) -> list[ChunkPlan]:
    plans: list[ChunkPlan] = []
    current: list[Segment] = []
    size = 0

    def flush():
        nonlocal current, size
        if current:
            blob = "␞".join(s.tagged for s in current)
            plans.append(ChunkPlan(chapters[current[0].sid], len(plans), [s.sid for s in current],
                                   hashlib.sha256(blob.encode()).hexdigest()))
        current, size = [], 0

    prev: Segment | None = None
    for seg in segments:
        if not seg.translatable:
            continue
        boundary = (
            prev is not None
            and (seg.part != prev.part or chapters[seg.sid] != chapters[prev.sid] or seg.heading_level is not None)
        )
        if current and (boundary or size + len(seg.tagged) > max_chars):
            flush()
        current.append(seg)
        size += len(seg.tagged)
        prev = seg
    flush()
    return plans
