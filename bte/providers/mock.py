"""A deterministic offline engine for tests and demos.

It understands every TASK the pipeline sends, returns well-formed answers, honours the
glossary, and can be told to fail in specific ways so the verification and repair logic
can be proven to catch each failure.
"""

from __future__ import annotations

import json
import re
from collections import Counter

from ..text import tags as T
from .base import LLMResult, SystemBlock

_PAYLOAD = re.compile(r"<payload>\n(.*)\n</payload>", re.DOTALL)
_PROTECT = re.compile(r"https?://\S+|[\w.+-]+@[\w-]+\.[\w.]+|\d+")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)

YO_VOWELS = str.maketrans({"a": "à", "e": "ẹ́", "i": "í", "o": "ọ̀", "u": "ú", "A": "À", "E": "Ẹ́", "I": "Í", "O": "Ọ̀", "U": "Ú"})


def pseudo_word(word: str, target: str) -> str:
    base = target.split("-")[0]
    if base == "yo":
        return word.translate(YO_VOWELS)
    if len(word) < 2:
        return word
    return word[::-1]


def pseudo_text(text: str, target: str, glossary: list[dict]) -> str:
    # Glossary terms first (protected from the word transform), then everything else.
    placeholders: list[str] = []

    def protect(value: str) -> str:
        placeholders.append(value)
        return f"\u0000{chr(0xE000 + len(placeholders) - 1)}\u0000"

    for entry in sorted(glossary, key=lambda e: -len(e.get("source", ""))):
        src, tgt = entry.get("source", ""), entry.get("target", "")
        if src and tgt:
            text = re.sub(rf"(?i)\b{re.escape(src)}\b", lambda _m, t=tgt: protect(t), text)
    text = _PROTECT.sub(lambda m: protect(m.group(0)), text)
    text = _WORD.sub(lambda m: pseudo_word(m.group(0), target), text)
    return re.sub("\u0000(.)\u0000", lambda m: placeholders[ord(m.group(1)) - 0xE000], text)


def pseudo_tagged(tagged: str, target: str, glossary: list[dict]) -> str:
    out = []
    for tok in T.tokenize(tagged):
        if tok.kind == "text":
            out.append(T.escape(pseudo_text(tok.value, target, glossary)))
        elif tok.kind == "open":
            out.append(f"<{tok.value}>")
        elif tok.kind == "close":
            out.append(f"</{tok.value}>")
        else:
            out.append(f"<{tok.value}/>")
    return "".join(out)


class MockLLM:
    provider = "mock"

    def __init__(self, family: str = "mock-a", model: str = "mock-1", faults: dict | None = None):
        self.family = family
        self.model = model
        self.faults: Counter = Counter(faults or {})
        self.calls: list[str] = []

    def _fault(self, name: str) -> bool:
        if self.faults[name] > 0:
            self.faults[name] -= 1
            return True
        return False

    async def complete(self, system: list[SystemBlock], user: str, *, temperature: float = 0.2,
                       max_tokens: int = 8000, json_mode: bool = True) -> LLMResult:
        task = system[0].text.split("\n", 1)[0].removeprefix("TASK: ").strip()
        self.calls.append(task)
        m = _PAYLOAD.search(user)
        data = json.loads(m.group(1)) if m else {}
        bible = _bible_from_system(system)
        target = _target_from_system(system[0].text)
        handler = getattr(self, f"_task_{task}")
        result = handler(data, bible, target)
        text = json.dumps(result, ensure_ascii=False)
        return LLMResult(text=text, input_tokens=len(user) // 4, output_tokens=len(text) // 4,
                         model=self.model, provider=self.provider)

    def _task_translate(self, data: dict, bible: dict, target: str) -> dict:
        glossary = list(bible.get("glossary", [])) + [
            {"source": e.get("name", ""), "target": e.get("target", "")} for e in bible.get("entities", [])
        ]
        segs = []
        for s in data.get("segments", []):
            text = pseudo_tagged(s["text"], target, glossary)
            if self._fault("drop_tags"):
                text = re.sub(r"</?[gk]\d+>|<x\d+/>", "", text)
            if self._fault("omit_sentence"):
                parts = re.split(r"(?<=[.!?])\s+", text)
                text = " ".join(parts[:-1]) if len(parts) > 1 else ""
            if self._fault("answer"):
                text = "Sure! Here is my answer: I think the market was busy."
            if self._fault("change_number"):
                text = re.sub(r"\d+", lambda mm: str(int(mm.group(0)) + 1), text, count=1)
            if self._fault("untranslated"):
                text = s["text"]
            segs.append({"id": s["id"], "translation": text})
        if self._fault("missing_segment") and segs:
            segs = segs[:-1]
        return {"segments": segs, "queries": []}

    def _task_refine(self, data: dict, bible: dict, target: str) -> dict:
        return {"segments": [{"id": r["id"], "translation": r["translation"]} for r in data.get("segments", [])],
                "queries": []}

    def _task_judge(self, data: dict, bible: dict, target: str) -> dict:
        rows = data.get("segments", [])
        is_translation = all(r["translation"] and r["translation"] != r["source"] for r in rows)
        errors = []
        if self._fault("judge_major"):
            errors.append({"segment": rows[0]["id"] if rows else "", "category": "accuracy", "severity": "major",
                           "source_span": "", "target_span": "", "explanation": "injected judge failure"})
        return {
            "is_translation": is_translation,
            "score": 92 if not errors else 60,
            "facts": [{"id": f["id"], "status": "present", "note": ""} for f in data.get("facts", [])],
            "errors": errors,
        }

    def _task_facts(self, data: dict, bible: dict, target: str) -> dict:
        facts = []
        for s in data.get("segments", []):
            plain = T.plain(s["text"])
            for num in re.findall(r"\d+", plain):
                facts.append({"id": f"f{len(facts) + 1}", "segment": s["id"], "type": "number", "text": num})
            if re.search(r"\bnot\b|n't", plain):
                facts.append({"id": f"f{len(facts) + 1}", "segment": s["id"], "type": "negation", "text": "not"})
        return {"facts": facts}

    def _task_bible_map(self, data: dict, bible: dict, target: str) -> dict:
        text = data.get("text", "")
        names = sorted({w for w in re.findall(r"(?<=[a-z,] )([A-Z][a-z]+)", text)})
        return {
            "summary": text[:200],
            "characters": [{"name": n, "gender": "unknown", "role": "", "address": ""} for n in names],
            "terms": [],
            "style": "neutral narrative",
            "cultural": [],
        }

    def _task_bible_reduce(self, data: dict, bible: dict, target: str) -> dict:
        entities = {}
        for part in data.get("parts", []):
            for c in part.get("characters", []):
                entities[c["name"]] = {"name": c["name"], "target": c["name"], "gender": c.get("gender", ""),
                                       "address": "", "note": ""}
        return {
            "book_summary": " ".join(p.get("summary", "")[:80] for p in data.get("parts", []))[:600],
            "style_guide": "Keep the neutral narrative register.",
            "glossary": [],
            "entities": list(entities.values()),
            "cultural_decisions": [],
        }


class MockNMT:
    provider = "mock_nmt"
    family = "mock-nmt"
    model = "mock-nmt-1"

    async def translate(self, texts: list[str], source: str, target: str) -> list[str]:
        return [pseudo_tagged(t, target, []) for t in texts]


def _bible_from_system(system: list[SystemBlock]) -> dict:
    for b in system:
        if b.text.startswith("BOOK BIBLE (approved"):
            try:
                return json.loads(b.text.split("\n", 1)[1])
            except (IndexError, json.JSONDecodeError):
                return {}
    return {}


_TGT = re.compile(r"into [^()]+\(([A-Za-z-]+)\)|to [^()]* book translations|into ([A-Za-z]+)\.")


def _target_from_system(text: str) -> str:
    m = re.search(r"into [^()\n]+? \(([A-Za-z0-9-]+)\)", text)
    return m.group(1) if m else "xx"
