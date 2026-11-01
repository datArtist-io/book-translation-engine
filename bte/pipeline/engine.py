"""The chunk processor: translate, verify, repair, refine, and decide.

For one chunk (a few paragraphs) it runs the repair ladder from the architecture doc:

  1. draft      drafter LLM, with Bible, glossary, context and memory
  2. fix        drafter again, told exactly which checks failed
  3. resample   drafter at higher temperature (most input-triggered failures vanish)
  4. alternate  a DIFFERENT model family (same-family retries rarely fix hallucinations)
  5. nmt        Google Cloud Translation as an independent engine
  -> human      if nothing passes, the best attempt goes to a reviewer with all evidence

Every attempt is checked by the deterministic gates (L0-L2), then by an independent
judge (L1, L5) that also confirms each extracted source fact survived. A chunk is
"verified" only when no blocking finding remains. Verified chunks may get guarded
refinement passes that are kept only if nothing gets worse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from ..languages import LanguageInfo
from ..providers.base import ProviderError
from ..text import tags as T
from . import gates, prompts
from .findings import SEVERITY_RANK, Finding
from .gateway import BudgetExceeded, Gateway

log = logging.getLogger(__name__)


@dataclass
class Engines:
    drafter: object
    judge: object | None = None
    alternate: object | None = None
    nmt: object | None = None


@dataclass
class EngineConfig:
    refine_passes: int = 1
    max_rungs: int = 5
    dual_path: bool = False  # independent second opinion for low-resource tiers


@dataclass
class Attempt:
    rung: str
    provider: str
    model: str
    family: str
    translations: dict[str, str]
    findings: list[Finding]
    judge: dict | None = None
    queries: list[dict] = field(default_factory=list)
    meta: dict = field(default_factory=dict)

    @property
    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.blocking]

    @property
    def passed(self) -> bool:
        return not self.blocking and self.judge is not None

    def rank(self) -> tuple:
        crit = sum(1 for f in self.findings if f.severity == "critical")
        major = sum(1 for f in self.findings if f.severity == "major")
        score = (self.judge or {}).get("score", 0) or 0
        return (crit, major, -score)

    def to_dict(self) -> dict:
        return {"rung": self.rung, "provider": self.provider, "model": self.model, "family": self.family,
                "translations": self.translations, "findings": [f.to_dict() for f in self.findings],
                "judge": self.judge, "queries": self.queries, "meta": self.meta}


@dataclass
class ChunkResult:
    state: str  # "verified" | "needs_human"
    translations: dict[str, str]
    attempts: list[Attempt]
    facts: list[dict]
    risk: int
    reasons: list[str]
    second_opinion: Attempt | None = None

    @property
    def final(self) -> Attempt:
        return self._final

    def set_final(self, a: Attempt) -> None:
        self._final = a


class ChunkProcessor:
    def __init__(self, engines: Engines, gateway: Gateway, src: LanguageInfo, tgt: LanguageInfo,
                 config: EngineConfig | None = None):
        self.e = engines
        self.gw = gateway
        self.src = src
        self.tgt = tgt
        self.cfg = config or EngineConfig()

    # ------------------------------------------------------------- helpers

    def _rungs(self) -> list[tuple[str, object, float]]:
        d = self.e.drafter
        rungs: list[tuple[str, object, float]] = [("draft", d, 0.2), ("fix", d, 0.3), ("resample", d, 0.8)]
        alt = self.e.alternate
        if alt is not None and getattr(alt, "family", None) != getattr(d, "family", None):
            rungs.append(("alternate", alt, 0.3))
        if self.e.nmt is not None:
            rungs.append(("nmt", self.e.nmt, 0.0))
        return rungs[: max(1, self.cfg.max_rungs)]

    async def facts(self, segments: list[dict]) -> list[dict]:
        llm = self.e.judge or self.e.drafter
        try:
            data, _ = await self.gw.llm_json(llm, prompts.facts_system(self.src), prompts.facts_user(segments),
                                             purpose="facts", temperature=0.0)
        except ProviderError as exc:
            log.warning("fact extraction failed: %s", exc)
            return []
        facts = [f for f in data.get("facts", []) if isinstance(f, dict) and f.get("id")]
        return facts[:80]

    async def _translate(self, rung: str, engine, temperature: float, segments: list[dict], context: dict,
                         bible: dict | None, previous: Attempt | None) -> Attempt:
        if rung == "nmt":
            texts, meta = await self.gw.nmt(engine, [s["text"] for s in segments], self.src.code, self.tgt.code)
            translations = {s["id"]: gates.normalize(t) for s, t in zip(segments, texts)}
            return Attempt(rung, meta["provider"], meta["model"], meta["family"], translations, [], meta=meta)
        issues = None
        prev = None
        if previous is not None and rung in {"fix", "resample", "alternate"}:
            prev = previous.translations
            issues = [{"segment": f.segment, "check": f.code, "severity": f.severity, "problem": f.message}
                      for f in previous.findings if f.blocking][:40]
        system = prompts.translate_system(self.src, self.tgt, bible)
        user = prompts.translate_user(segments, context, previous=prev, issues=issues)
        data, meta = await self.gw.llm_json(engine, system, user, purpose=f"translate:{rung}", temperature=temperature)
        translations: dict[str, str] = {}
        for item in data.get("segments", []):
            if isinstance(item, dict) and isinstance(item.get("id"), str) and isinstance(item.get("translation"), str):
                translations[item["id"]] = gates.normalize(item["translation"])
        queries = [q for q in data.get("queries", []) if isinstance(q, dict)]
        return Attempt(rung, meta["provider"], meta["model"], meta["family"], translations, [], queries=queries,
                       meta=meta)

    def _gate(self, attempt: Attempt, segments: list[dict], glossary: list[dict]) -> None:
        f = gates.expected_ids(segments, attempt.translations)
        for s in segments:
            t = attempt.translations.get(s["id"])
            if t is not None:
                f += gates.check_segment(s["id"], s["text"], t, self.src, self.tgt, glossary)
        f += gates.check_chunk_orthography(list(attempt.translations.values()), self.tgt)
        attempt.findings.extend(f)

    async def _judge(self, attempt: Attempt, segments: list[dict], facts: list[dict], bible: dict | None) -> None:
        judge = self.e.judge
        if judge is None:
            attempt.findings.append(Finding("L5", "judge_unavailable", "info",
                                            "No independent judge configured; a human must review."))
            return
        if getattr(judge, "family", None) == attempt.family:
            attempt.findings.append(Finding("L5", "judge_same_family", "info",
                                            "The judge is from the same model family as the translator."))
        try:
            data, meta = await self.gw.llm_json(
                judge, prompts.judge_system(self.src, self.tgt, bible),
                prompts.judge_user(segments, attempt.translations, facts), purpose="judge", temperature=0.0)
        except ProviderError as exc:
            attempt.findings.append(Finding("L5", "judge_failed", "info", f"Judge call failed: {exc}"))
            return
        attempt.judge = {"score": data.get("score"), "is_translation": data.get("is_translation", True),
                         "model": meta.get("model"), "provider": meta.get("provider")}
        if data.get("is_translation") is False:
            attempt.findings.append(Finding("L1", "not_translation", "critical",
                                            "The judge found the output is not a translation."))
        fact_index = {f["id"]: f for f in facts}
        for fs in data.get("facts", []) or []:
            if not isinstance(fs, dict):
                continue
            status = fs.get("status")
            if status in {"missing", "changed"}:
                fact = fact_index.get(fs.get("id"), {})
                severe = fact.get("type") in {"number", "negation", "date", "quantity", "condition", "modality"}
                attempt.findings.append(Finding(
                    "L5", f"fact_{status}", "critical" if severe else "major",
                    f"Fact {status}: {fact.get('text', fs.get('id'))}. {fs.get('note', '')}".strip(),
                    fact.get("segment"), {"fact": fact}))
        for err in data.get("errors", []) or []:
            if not isinstance(err, dict):
                continue
            sev = err.get("severity", "minor")
            if sev not in SEVERITY_RANK:
                sev = "major"
            msg = err.get("explanation") or err.get("category", "issue")
            spans = f" [source: {err.get('source_span', '')!s:.80} | target: {err.get('target_span', '')!s:.80}]"
            attempt.findings.append(Finding("L5", err.get("category", "judge"), sev, msg + spans, err.get("segment")))

    async def _verify(self, attempt: Attempt, segments, glossary, facts, bible) -> None:
        self._gate(attempt, segments, glossary)
        if not any(f.severity == "critical" for f in attempt.findings) or attempt.rung == "nmt":
            await self._judge(attempt, segments, facts, bible)

    # ------------------------------------------------------------- main

    async def process(self, segments: list[dict], context: dict, bible: dict | None,
                      glossary: list[dict]) -> ChunkResult:
        facts = await self.facts(segments)
        attempts: list[Attempt] = []
        best: Attempt | None = None
        passed: Attempt | None = None
        previous: Attempt | None = None
        for rung, engine, temp in self._rungs():
            try:
                attempt = await self._translate(rung, engine, temp, segments, context, bible, previous)
            except BudgetExceeded:
                raise
            except ProviderError as exc:
                attempt = Attempt(rung, getattr(engine, "provider", "?"), getattr(engine, "model", "?"),
                                  getattr(engine, "family", "?"), {},
                                  [Finding("L0", "provider_error", "critical", str(exc))])
                attempts.append(attempt)
                continue
            await self._verify(attempt, segments, glossary, facts, bible)
            attempts.append(attempt)
            if best is None or attempt.rank() < best.rank():
                best = attempt
            if attempt.passed:
                passed = attempt
                break
            previous = attempt

        if passed is not None:
            passed = await self._refine(passed, segments, glossary, facts, bible, attempts)

        final = passed or best
        second = None
        if self.cfg.dual_path and final is not None and final.translations:
            second = await self._second_opinion(final, segments, glossary, facts, bible, context)

        reasons: list[str] = []
        if passed is None:
            reasons.append("No attempt passed every check; a human must decide.")
        if final is None:
            final = Attempt("none", "-", "-", "-", {}, [Finding("L0", "no_attempt", "critical", "No engine produced output.")])
        risk = self._risk(final, attempts, second, passed is not None)
        result = ChunkResult(
            state="verified" if passed is not None else "needs_human",
            translations=dict(final.translations),
            attempts=attempts,
            facts=facts,
            risk=risk,
            reasons=reasons,
            second_opinion=second,
        )
        result.set_final(final)
        return result

    async def _refine(self, current: Attempt, segments, glossary, facts, bible, attempts) -> Attempt:
        for i in range(self.cfg.refine_passes):
            try:
                data, meta = await self.gw.llm_json(
                    self.e.drafter, prompts.refine_system(self.src, self.tgt, bible),
                    prompts.refine_user(segments, current.translations), purpose=f"refine:{i}", temperature=0.3)
            except ProviderError:
                break
            translations = {it["id"]: gates.normalize(it["translation"]) for it in data.get("segments", [])
                            if isinstance(it, dict) and isinstance(it.get("translation"), str) and it.get("id")}
            if translations == current.translations or set(translations) != set(current.translations):
                break
            cand = Attempt(f"refine{i + 1}", meta["provider"], meta["model"], meta["family"], translations, [],
                           meta=meta)
            await self._verify(cand, segments, glossary, facts, bible)
            attempts.append(cand)
            old = (current.judge or {}).get("score") or 0
            new = (cand.judge or {}).get("score") or 0
            if cand.passed and new >= old:
                current = cand
            else:
                break
        return current

    async def _second_opinion(self, final: Attempt, segments, glossary, facts, bible, context) -> Attempt | None:
        engine = None
        for cand in (self.e.alternate, self.e.nmt):
            if cand is not None and getattr(cand, "family", None) != final.family:
                engine = cand
                break
        if engine is None:
            return None
        rung = "nmt" if engine is self.e.nmt else "alternate"
        try:
            att = await self._translate(rung, engine, 0.2, segments, context, bible, None)
        except ProviderError:
            return None
        att.rung = "second_opinion"
        self._gate(att, segments, glossary)
        return att

    def _risk(self, final: Attempt, attempts: list[Attempt], second: Attempt | None, passed: bool) -> int:
        risk = 0
        if not passed:
            risk += 45
        risk += 6 * max(0, len([a for a in attempts if not a.rung.startswith("refine")]) - 1)
        score = (final.judge or {}).get("score")
        risk += int((100 - score) / 2) if isinstance(score, (int, float)) else 25
        if any(f.code in {"judge_same_family", "judge_unavailable", "judge_failed"} for f in final.findings):
            risk += 15
        if self.tgt.tier not in {"A", "B"}:
            risk += 15
        risk += 5 * len(final.queries)
        if second is not None:
            for s, t in final.translations.items():
                o = second.translations.get(s)
                if o is not None and gates.digit_runs(T.plain(o)) != gates.digit_runs(T.plain(t)):
                    risk += 10
                    break
        return max(0, min(100, risk))
