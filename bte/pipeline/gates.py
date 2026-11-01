"""L0-L2: deterministic checks that run on every translated segment, for every language.

Each check targets a specific, documented failure of machine translation:
missing or broken formatting tags, changed numbers, untranslated or wrong-script output,
answers instead of translations, oscillating repetition, glossary drift, missing tone
marks, and omitted or added sentences.
"""

from __future__ import annotations

import re
import unicodedata
from collections import Counter

from ..languages import LanguageInfo, char_script_matches
from ..text import tags as T
from ..text.align import coverage_issues
from .findings import Finding

URL_RE = re.compile(r"https?://[^\s<>\"']+|www\.[^\s<>\"']+|[\w.+-]+@[\w-]+\.[\w.-]+")
WORD_RE = re.compile(r"[^\W\d_]+", re.UNICODE)

ANSWER_MARKERS = [
    r"^\s*(sure|certainly|of course)[,!.]",
    r"\bas an ai\b",
    r"\bi('m| am) (sorry|unable|not able)\b",
    r"\bi can(no|')t (help|assist|translate|do)\b",
    r"^\s*here('s| is) (the|your|my) (translation|answer)",
    r"^\s*translation\s*:",
]


def normalize(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def digit_runs(text: str) -> Counter:
    """Digit runs, script-independent (Arabic-Indic and other digits are normalized).

    Separators are ignored, so 1,000.50 and 1.000,50 both give {1, 000, 50}.
    """
    norm = []
    for ch in text:
        if ch.isdigit():
            try:
                norm.append(str(unicodedata.digit(ch)))
            except (TypeError, ValueError):
                norm.append(" ")
        else:
            norm.append(" ")
    return Counter("".join(norm).split())


def script_share(text: str, script: str | None) -> float | None:
    if not script:
        return None
    letters = [ch for ch in text if unicodedata.category(ch).startswith("L")]
    if len(letters) < 8:
        return None
    verdicts = [char_script_matches(ch, script) for ch in letters]
    if any(v is None for v in verdicts):
        return None
    return sum(1 for v in verdicts if v) / len(letters)


def diacritic_density(text: str, lang: LanguageInfo) -> float | None:
    prof = lang.diacritics
    if prof is None:
        return None
    words = WORD_RE.findall(text)
    if len(words) < prof.min_words:
        return None
    marked = 0
    for wd in words:
        nfd = unicodedata.normalize("NFD", wd)
        if any(c in prof.marks for c in nfd) or any(c in prof.letters for c in wd):
            marked += 1
    return marked / len(words)


def repetition(text: str) -> str | None:
    words = text.lower().split()
    if len(words) < 12:
        return None
    grams = Counter(tuple(words[i : i + 3]) for i in range(len(words) - 2))
    gram, n = grams.most_common(1)[0]
    if n >= 4 and n * 3 > len(words) * 0.3:
        return " ".join(gram)
    return None


def check_segment(sid: str, source_tagged: str, target_tagged: str, src: LanguageInfo, tgt: LanguageInfo,
                  glossary: list[dict], expansion: float | None = None) -> list[Finding]:
    out: list[Finding] = []
    src_plain = T.plain(source_tagged)
    tgt_plain = T.plain(target_tagged)

    if not tgt_plain.strip():
        out.append(Finding("L0", "empty", "critical", "The translation is empty.", sid))
        return out

    problems = T.validate(target_tagged, T.tag_ids(source_tagged))
    for p in problems:
        out.append(Finding("L0", "tags", "major", f"Formatting tag problem: {p}.", sid))
    spans = T.span_texts(target_tagged)
    src_spans = T.span_texts(source_tagged)
    for tid, txt in spans.items():
        if not txt.strip() and src_spans.get(tid, "").strip():
            out.append(Finding("L0", "empty_span", "major",
                               f"Formatting {tid} lost its words (the formatted text is empty).", sid))

    sd, td = digit_runs(src_plain), digit_runs(tgt_plain)
    if sd != td:
        missing = list((sd - td).elements())
        extra = list((td - sd).elements())
        out.append(Finding("L0", "numbers", "critical",
                           f"Numbers differ from the source (missing {missing or 'none'}, added {extra or 'none'}).",
                           sid, {"missing": missing, "added": extra}))

    for url in URL_RE.findall(src_plain):
        if url.rstrip(".,;:") not in tgt_plain:
            out.append(Finding("L0", "url", "major", f"Link or email not preserved: {url}", sid))

    low_src, low_tgt = src_plain.lower(), tgt_plain.lower()
    for entry in glossary:
        s, t = (entry.get("source") or "").strip(), (entry.get("target") or "").strip()
        if not s or not t:
            continue
        if re.search(rf"(?<!\w){re.escape(s.lower())}(?!\w)", low_src) and t.lower() not in low_tgt:
            out.append(Finding("L0", "glossary", "major",
                               f"Glossary term '{s}' must be translated as '{t}'.", sid, {"source": s, "target": t}))

    for pat in ANSWER_MARKERS:
        if re.search(pat, tgt_plain, re.IGNORECASE) and not re.search(pat, src_plain, re.IGNORECASE):
            out.append(Finding("L1", "not_translation", "critical",
                               "The output looks like an answer or refusal, not a translation.", sid))
            break

    share = script_share(tgt_plain, tgt.script)
    if share is not None and share < 0.6:
        out.append(Finding("L0", "script", "critical",
                           f"Only {share:.0%} of letters are in the expected {tgt.script} script.", sid))

    src_words = WORD_RE.findall(low_src)
    tgt_words = WORD_RE.findall(low_tgt)
    if src.code.split("-")[0] != tgt.code.split("-")[0] and len(tgt_words) >= 8:
        src_set = {x for x in src_words if len(x) >= 4}
        long_tgt = [x for x in tgt_words if len(x) >= 4]
        if long_tgt:
            same = sum(1 for x in long_tgt if x in src_set) / len(long_tgt)
            if same > 0.6:
                out.append(Finding("L0", "untranslated", "critical",
                                   f"{same:.0%} of the words are identical to the source; the text looks untranslated.", sid))

    if len(src_plain) >= 20:
        ratio = len(tgt_plain) / max(len(src_plain), 1)
        lo, hi = (0.25, 3.0) if expansion is None else (expansion * 0.45, expansion * 2.2)
        if tgt.script in {"Hans", "Hant", "Jpan", "Kore", "Hang"} and expansion is None:
            lo = 0.1  # logographic and syllabic scripts are far shorter in characters
        if src.script in {"Hans", "Hant", "Jpan", "Kore", "Hang"} and expansion is None:
            hi = 8.0
        if not lo <= ratio <= hi:
            out.append(Finding("L0", "length", "major",
                               f"Length ratio {ratio:.2f} is outside the expected range {lo:.2f}-{hi:.2f}.", sid,
                               {"ratio": ratio}))

    rep = repetition(tgt_plain)
    if rep and not repetition(src_plain):
        out.append(Finding("L0", "repetition", "major", f"Repeated phrase loop: '{rep}'.", sid))

    cov = coverage_issues(src_plain, tgt_plain, expansion)
    for sentence in cov["omitted"]:
        out.append(Finding("L2", "omission", "major", f"Source sentence may be missing: “{sentence[:160]}”",
                           sid, {"sentence": sentence}))
    for sentence in cov["added"]:
        out.append(Finding("L2", "addition", "major", f"Translation sentence may be added: “{sentence[:160]}”",
                           sid, {"sentence": sentence}))
    return out


def check_chunk_orthography(texts: list[str], tgt: LanguageInfo) -> list[Finding]:
    joined = " ".join(T.plain(t) for t in texts)
    density = diacritic_density(joined, tgt)
    prof = tgt.diacritics
    if density is not None and prof is not None and density < prof.min_density:
        return [Finding("L0", "diacritics", prof.severity,
                        f"Only {density:.0%} of words carry the marks {tgt.name} normally needs "
                        f"(expected at least {prof.min_density:.0%}); tone marks or special letters may be missing.",
                        None, {"density": density})]
    return []


def expected_ids(segments: list[dict], returned: dict[str, str]) -> list[Finding]:
    out = []
    ids = {s["id"] for s in segments}
    for sid in ids - returned.keys():
        out.append(Finding("L0", "missing_segment", "critical", "This segment was not returned by the translator.", sid))
    for sid in returned.keys() - ids:
        out.append(Finding("L0", "extra_segment", "major", f"Unexpected segment id {sid} in the reply.", sid))
    return out
