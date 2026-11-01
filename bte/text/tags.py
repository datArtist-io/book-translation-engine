"""Inline placeholder tags that carry formatting through translation.

A paragraph's text is sent to translation as *tagged text*:

    The <g1>red</g1> house, see <k2>our site</k2><x3/>.

  <gN>...</gN>  a formatting span (bold, italic, colour, font...) around words
  <kN>...</kN>  a wrapper (hyperlink, content control, smart tag) around words
  <xN/>         an opaque object kept verbatim (tab, line break, image, field code,
                footnote reference, bookmark)

Literal "&", "<" and ">" in the text are escaped as &amp; &lt; &gt;.
The translator must return every tag exactly once, properly nested, around the
target words that carry the same meaning. These helpers parse and validate that.
"""

from __future__ import annotations

import html
import re
from collections import Counter
from dataclasses import dataclass

TAG_RE = re.compile(r"<(/?)([gk])(\d+)>|<x(\d+)/>")


@dataclass(frozen=True)
class Tok:
    kind: str  # "text" | "open" | "close" | "empty"
    value: str  # text, or tag id like "g1" / "x3"


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def unescape(text: str) -> str:
    return html.unescape(text)


def tokenize(tagged: str) -> list[Tok]:
    toks: list[Tok] = []
    pos = 0
    for m in TAG_RE.finditer(tagged):
        if m.start() > pos:
            toks.append(Tok("text", unescape(tagged[pos : m.start()])))
        if m.group(4) is not None:
            toks.append(Tok("empty", f"x{m.group(4)}"))
        else:
            tid = f"{m.group(2)}{m.group(3)}"
            toks.append(Tok("close" if m.group(1) else "open", tid))
        pos = m.end()
    if pos < len(tagged):
        toks.append(Tok("text", unescape(tagged[pos:])))
    return toks


def plain(tagged: str) -> str:
    """Text with all tags removed and entities unescaped."""
    return "".join(t.value for t in tokenize(tagged) if t.kind == "text")


def tag_ids(tagged: str) -> Counter:
    """Multiset of tag ids as they appear (opening/empty tags only)."""
    return Counter(t.value for t in tokenize(tagged) if t.kind in {"open", "empty"})


def validate(tagged: str, expected: Counter) -> list[str]:
    """Return a list of human-readable tag problems; empty list means valid."""
    problems: list[str] = []
    stack: list[str] = []
    seen: Counter = Counter()
    for t in tokenize(tagged):
        if t.kind == "open":
            stack.append(t.value)
            seen[t.value] += 1
        elif t.kind == "empty":
            seen[t.value] += 1
        elif t.kind == "close":
            if not stack:
                problems.append(f"closing tag </{t.value}> without an opening tag")
            elif stack[-1] != t.value:
                problems.append(f"tags cross: </{t.value}> closes while <{stack[-1]}> is open")
                if t.value in stack:
                    stack.remove(t.value)
            else:
                stack.pop()
    for tid in stack:
        problems.append(f"tag <{tid}> is never closed")
    for tid, n in expected.items():
        if seen[tid] < n:
            problems.append(f"tag {tid} is missing")
    for tid, n in seen.items():
        if n > expected.get(tid, 0):
            problems.append(f"tag {tid} is unexpected or duplicated")
    return problems


def span_texts(tagged: str) -> dict[str, str]:
    """Plain text inside each g/k tag (for formatting-placement checks)."""
    out: dict[str, list[str]] = {}
    stack: list[str] = []
    for t in tokenize(tagged):
        if t.kind == "open":
            stack.append(t.value)
            out.setdefault(t.value, [])
        elif t.kind == "close":
            if t.value in stack:
                stack.remove(t.value)
        elif t.kind == "text":
            for tid in stack:
                out[tid].append(t.value)
    return {k: "".join(v) for k, v in out.items()}
