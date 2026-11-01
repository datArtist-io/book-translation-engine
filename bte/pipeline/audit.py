"""L6: book-level audit across all chunks, for consistency no single chunk can see."""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from ..languages import LanguageInfo
from ..text import tags as T
from .findings import Finding

NAME_RE = re.compile(r"(?<=[\w,;:] )([A-Z][a-zÀ-ɏ]{2,})")


def name_consistency(chunks: list[tuple[int, dict[str, str], dict[str, str]]], src: LanguageInfo,
                     tgt: LanguageInfo) -> dict[int, list[Finding]]:
    """Names kept verbatim in most chunks but rendered differently in a few are flagged.

    chunks: (chunk_id, {sid: source_tagged}, {sid: target_tagged})
    """
    out: dict[int, list[Finding]] = defaultdict(list)
    if src.script != tgt.script:
        return out  # transliteration is expected across scripts
    occurrences: dict[str, list[tuple[int, bool]]] = defaultdict(list)
    freq: Counter = Counter()
    for cid, sources, targets in chunks:
        src_text = " ".join(T.plain(v) for v in sources.values())
        tgt_text = " ".join(T.plain(v) for v in targets.values())
        names = set(NAME_RE.findall(src_text))
        for name in names:
            freq[name] += 1
            occurrences[name].append((cid, re.search(rf"\b{re.escape(name)}\b", tgt_text) is not None))
    for name, occ in occurrences.items():
        if freq[name] < 3:
            continue
        kept = sum(1 for _, k in occ if k)
        if kept / len(occ) >= 0.6 and kept < len(occ):
            for cid, k in occ:
                if not k:
                    out[cid].append(Finding("L6", "name_consistency", "major",
                                            f"'{name}' is kept as-is in {kept} of {len(occ)} chunks but rendered "
                                            "differently here.", None, {"name": name}))
    return out


def glossary_drift(chunks: list[tuple[int, dict[str, str], dict[str, str]]],
                   glossary: list[dict]) -> dict[int, list[Finding]]:
    out: dict[int, list[Finding]] = defaultdict(list)
    for cid, sources, targets in chunks:
        for sid, src in sources.items():
            s_plain, t_plain = T.plain(src).lower(), T.plain(targets.get(sid, "")).lower()
            for e in glossary:
                term, want = (e.get("source") or "").lower(), (e.get("target") or "").lower()
                if term and want and re.search(rf"(?<!\w){re.escape(term)}(?!\w)", s_plain) and want not in t_plain:
                    out[cid].append(Finding("L6", "glossary", "major",
                                            f"Glossary term '{e['source']}' must be '{e['target']}'.", sid))
    return out
