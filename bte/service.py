"""The book workflow: ingest -> Book Bible -> approval -> translate -> audit -> review -> release -> export."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select

from .config import Settings, get_settings
from .docx.segments import extract, formatting_inventory, rebuild
from .languages import LanguageInfo, UnknownLanguage, get_language
from .pipeline import audit, gates, prompts
from .pipeline.chunking import NOTES_CHAPTER, assign_chapters, plan_chunks
from .pipeline.engine import ChunkProcessor, EngineConfig, Engines
from .pipeline.gateway import BudgetExceeded, Gateway
from .pipeline.release import SampleItem, decide, draw_sample, required_sentences
from .providers.base import ProviderError
from .providers.registry import build_llm, build_nmt
from .render import render_pdf_pages
from .store import (AttemptRow, Bible, Chunk, DbCache, GlossaryEntry, Project, Review, SegmentRow, Store,
                    glossary_for)
from .text import tags as T
from .text.align import split_sentences

log = logging.getLogger(__name__)


class WorkflowError(ValueError):
    """A user-facing error: the message explains what to do."""


@dataclass
class ExportResult:
    path: Path
    label: str  # VERIFIED | DRAFT
    fidelity: dict
    skipped: list[str]


class Service:
    def __init__(self, settings: Settings | None = None, store: Store | None = None, engines: Engines | None = None):
        self.settings = settings or get_settings()
        self.store = store or Store(self.settings.db_url)
        self._engines = engines
        self.root = Path(self.settings.data_dir) / "projects"

    # ------------------------------------------------------------ setup

    def engines(self) -> Engines:
        if self._engines is None:
            s = self.settings
            drafter = build_llm(s.drafter, s)
            if drafter is None:
                raise WorkflowError("No translation model is configured (BTE_DRAFTER).")
            self._engines = Engines(drafter=drafter, judge=build_llm(s.judge, s),
                                    alternate=build_llm(s.alternate, s), nmt=build_nmt(s.nmt, s))
        return self._engines

    def project_dir(self, pid: int) -> Path:
        d = self.root / str(pid)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def source_bytes(self, pid: int) -> bytes:
        return (self.project_dir(pid) / "source.docx").read_bytes()

    # ------------------------------------------------------------ ingest

    def create_project(self, data: bytes, filename: str, source: str, target: str, actor: str = "user") -> int:
        if len(data) > self.settings.max_upload_mb * 1024 * 1024:
            raise WorkflowError(f"The file is larger than the {self.settings.max_upload_mb} MB limit.")
        if not filename.lower().endswith(".docx"):
            raise WorkflowError("This version accepts .docx files. Other formats come in later releases.")
        try:
            src, tgt = get_language(source), get_language(target)
        except UnknownLanguage as exc:
            raise WorkflowError(str(exc)) from exc
        if src.code.split("-")[0] == tgt.code.split("-")[0]:
            raise WorkflowError("Source and target languages must be different.")
        ex = extract(data)  # raises DocxRejected with a user-safe message
        if not any(s.translatable for s in ex.segments):
            raise WorkflowError("No translatable text was found in this document.")
        chapters = assign_chapters(ex.segments)
        plans = plan_chunks(ex.segments, chapters, self.settings.chunk_chars)
        s = self.settings
        with self.store.session() as db:
            project = Project(
                name=Path(filename).stem[:300], filename=Path(filename).name[:300],
                source_lang=src.code, target_lang=tgt.code, tier=tgt.tier, review_policy=tgt.review_policy,
                stage="bible_draft", source_sha256=hashlib.sha256(data).hexdigest(), warnings=ex.warnings,
                models={"drafter": s.drafter, "judge": s.judge, "alternate": s.alternate, "nmt": s.nmt,
                        "prompt_version": prompts_version()},
            )
            db.add(project)
            db.flush()
            pid = project.id
            chunk_ids: dict[str, int] = {}
            for plan in plans:
                ch = Chunk(project_id=pid, chapter=plan.chapter, order=plan.order, sids=plan.sids,
                           source_hash=plan.source_hash)
                db.add(ch)
                db.flush()
                for sid in plan.sids:
                    chunk_ids[sid] = ch.id
            for seg in ex.segments:
                db.add(SegmentRow(project_id=pid, sid=seg.sid, part=seg.part, ordinal=seg.ordinal, tagged=seg.tagged,
                                  plain=seg.plain, translatable=seg.translatable, style=seg.style,
                                  heading_level=seg.heading_level, tag_labels=seg.tag_labels, tag_keys=seg.tag_keys,
                                  chapter=chapters.get(seg.sid, 0), chunk_id=chunk_ids.get(seg.sid)))
        (self.project_dir(pid) / "source.docx").write_bytes(data)
        self.store.log(pid, actor, "project_created", filename=filename, source=src.code, target=tgt.code,
                       segments=len(ex.segments), chunks=len(plans))
        return pid

    def languages(self, pid: int) -> tuple[LanguageInfo, LanguageInfo]:
        with self.store.session() as db:
            p = db.get(Project, pid)
            if p is None:
                raise WorkflowError("Project not found.")
            return get_language(p.source_lang), get_language(p.target_lang)

    def _set_stage(self, pid: int, stage: str, detail: str = "") -> None:
        with self.store.session() as db:
            p = db.get(Project, pid)
            p.stage, p.stage_detail = stage, detail

    # ------------------------------------------------------------ Book Bible

    async def build_bible(self, pid: int) -> dict:
        src, tgt = self.languages(pid)
        engines = self.engines()
        llm = engines.drafter
        gw = Gateway(cache=DbCache(self.store), concurrency=self.settings.provider_concurrency,
                     token_budget=self.settings.job_token_budget)
        with self.store.session() as db:
            rows = db.scalars(select(SegmentRow).where(SegmentRow.project_id == pid, SegmentRow.translatable)
                              .order_by(SegmentRow.id)).all()
            texts: dict[int, list[str]] = defaultdict(list)
            for r in rows:
                texts[r.chapter].append(r.plain)
        parts: list[tuple[int, str]] = []
        for ch in sorted(texts):
            buf = ""
            for para in texts[ch]:
                if len(buf) + len(para) > 12000 and buf:
                    parts.append((ch, buf))
                    buf = ""
                buf += para + "\n"
            if buf:
                parts.append((ch, buf))

        sem = asyncio.Semaphore(self.settings.provider_concurrency)

        async def map_part(i: int, ch: int, text: str):
            async with sem:
                try:
                    data, _ = await gw.llm_json(llm, prompts.bible_map_system(src), prompts.bible_map_user(text, i),
                                                purpose="bible_map", temperature=0.0)
                except ProviderError as exc:
                    log.warning("bible map failed for part %s: %s", i, exc)
                    data = {}
                return ch, data

        mapped = await asyncio.gather(*(map_part(i, ch, t) for i, (ch, t) in enumerate(parts)))
        chapter_summaries: dict[str, str] = defaultdict(str)
        notes = []
        for ch, data in mapped:
            if ch != NOTES_CHAPTER and data.get("summary"):
                chapter_summaries[str(ch)] += (" " if chapter_summaries[str(ch)] else "") + str(data["summary"])
            notes.append({k: data.get(k) for k in ("summary", "characters", "terms", "style", "cultural")})
        try:
            reduced, _ = await gw.llm_json(llm, prompts.bible_reduce_system(src, tgt),
                                           prompts.bible_reduce_user(notes), purpose="bible_reduce", temperature=0.0)
        except ProviderError as exc:
            raise WorkflowError(f"Could not build the Book Bible: {exc}") from exc
        content = {
            "book_summary": reduced.get("book_summary", ""),
            "style_guide": reduced.get("style_guide", ""),
            "glossary": [g for g in reduced.get("glossary", []) if isinstance(g, dict) and g.get("source")],
            "entities": [e for e in reduced.get("entities", []) if isinstance(e, dict) and e.get("name")],
            "cultural_decisions": reduced.get("cultural_decisions", []),
            "chapters": dict(chapter_summaries),
        }
        with self.store.session() as db:
            version = (db.scalar(select(func.max(Bible.version)).where(Bible.project_id == pid)) or 0) + 1
            db.add(Bible(project_id=pid, version=version, content=content, approved=False))
            p = db.get(Project, pid)
            p.stage, p.stage_detail = "bible_review", "The Book Bible is ready for your review and approval."
            usage = dict(p.usage or {})
            usage["bible_tokens"] = gw.usage.total()
            p.usage = usage
        self.store.log(pid, "system", "bible_drafted", version=version)
        return content

    def latest_bible(self, pid: int, approved_only: bool = False) -> Bible | None:
        with self.store.session() as db:
            q = select(Bible).where(Bible.project_id == pid)
            if approved_only:
                q = q.where(Bible.approved)
            return db.scalars(q.order_by(Bible.version.desc())).first()

    def approve_bible(self, pid: int, content: dict, actor: str) -> None:
        glossary = [g for g in content.get("glossary", []) if (g.get("source") or "").strip() and (g.get("target") or "").strip()]
        content = {**content, "glossary": glossary}
        with self.store.session() as db:
            version = (db.scalar(select(func.max(Bible.version)).where(Bible.project_id == pid)) or 0) + 1
            db.add(Bible(project_id=pid, version=version, content=content, approved=True, approved_by=actor))
            for row in db.scalars(select(GlossaryEntry).where(GlossaryEntry.project_id == pid,
                                                              GlossaryEntry.origin == "bible")).all():
                db.delete(row)
            for g in glossary:
                db.add(GlossaryEntry(project_id=pid, source=g["source"].strip(), target=g["target"].strip(),
                                     note=g.get("note", ""), origin="bible"))
            for e in content.get("entities", []):
                name, tgt = (e.get("name") or "").strip(), (e.get("target") or "").strip()
                if name and tgt:
                    db.add(GlossaryEntry(project_id=pid, source=name, target=tgt, note="entity", origin="bible"))
            p = db.get(Project, pid)
            p.stage, p.stage_detail = "translating", "Queued for translation."
        self.store.log(pid, actor, "bible_approved", version=version, glossary=len(glossary))

    # ------------------------------------------------------------ translation

    async def translate(self, pid: int) -> None:
        bible = self.latest_bible(pid, approved_only=True)
        if bible is None:
            raise WorkflowError("Approve the Book Bible before translating.")
        src, tgt = self.languages(pid)
        with self.store.session() as db:
            p = db.get(Project, pid)
            used = int((p.usage or {}).get("tokens", 0))
            p.stage, p.stage_detail = "translating", "Translating and verifying."
            chunks = db.scalars(select(Chunk).where(Chunk.project_id == pid).order_by(Chunk.chapter, Chunk.order)).all()
            pending = [(c.id, c.chapter) for c in chunks if c.state == "pending"]
        gw = Gateway(cache=DbCache(self.store), concurrency=self.settings.provider_concurrency,
                     token_budget=max(0, self.settings.job_token_budget - used))
        processor = ChunkProcessor(self.engines(), gw, src, tgt, EngineConfig(
            refine_passes=self.settings.refine_passes, max_rungs=self.settings.max_repair_rungs,
            dual_path=tgt.tier not in {"A", "B"}))
        bible_prompt = {k: v for k, v in bible.content.items() if k != "chapters"}
        chapter_summaries = bible.content.get("chapters", {})
        by_chapter: dict[int, list[int]] = defaultdict(list)
        for cid, ch in pending:
            by_chapter[ch].append(cid)
        sem = asyncio.Semaphore(max(1, self.settings.provider_concurrency))

        async def run_chapter(chunk_ids: list[int]):
            async with sem:
                for cid in chunk_ids:
                    await self._process_chunk(pid, cid, processor, bible_prompt, chapter_summaries)

        try:
            await asyncio.gather(*(run_chapter(ids) for ids in by_chapter.values()))
        except BudgetExceeded as exc:
            self._save_usage(pid, gw)
            self._set_stage(pid, "paused", str(exc))
            self.store.log(pid, "system", "paused_budget")
            return
        self._save_usage(pid, gw)
        self.after_translation(pid)

    def _save_usage(self, pid: int, gw: Gateway) -> None:
        with self.store.session() as db:
            p = db.get(Project, pid)
            usage = dict(p.usage or {})
            usage["tokens"] = int(usage.get("tokens", 0)) + gw.usage.total()
            usage["input_tokens"] = int(usage.get("input_tokens", 0)) + gw.usage.input_tokens
            usage["output_tokens"] = int(usage.get("output_tokens", 0)) + gw.usage.output_tokens
            usage["cached_tokens"] = int(usage.get("cached_tokens", 0)) + gw.usage.cached_tokens
            usage["calls"] = int(usage.get("calls", 0)) + gw.usage.calls
            usage["cache_hits"] = int(usage.get("cache_hits", 0)) + gw.usage.cache_hits
            p.usage = usage

    def _context(self, db, pid: int, chunk: Chunk, chapter_summaries: dict) -> dict:
        same = db.scalars(select(Chunk).where(Chunk.project_id == pid, Chunk.chapter == chunk.chapter)
                          .order_by(Chunk.order)).all()
        idx = [c.id for c in same].index(chunk.id)
        seg_text = {r.sid: r.tagged for r in db.scalars(
            select(SegmentRow).where(SegmentRow.project_id == pid, SegmentRow.chapter == chunk.chapter)).all()}
        previous = []
        for c in same[max(0, idx - 2):idx]:
            if c.translations:
                previous.append([{"source": seg_text.get(s, ""), "translation": c.translations.get(s, "")}
                                 for s in c.sids][-3:])
        nxt = ""
        if idx + 1 < len(same):
            nxt = " ".join(T.plain(seg_text.get(s, "")) for s in same[idx + 1].sids)[:1500]
        examples = self._examples(db, pid, chunk, seg_text)
        ctx = {"chapter_summary": chapter_summaries.get(str(chunk.chapter), ""),
               "previous_passages": previous, "next_source": nxt, "approved_examples": examples}
        return {k: v for k, v in ctx.items() if v}

    def _examples(self, db, pid: int, chunk: Chunk, seg_text: dict) -> list[dict]:
        """Translation memory: the most similar human-approved passages (word overlap)."""
        words = set(re.findall(r"\w{4,}", " ".join(T.plain(seg_text.get(s, "")) for s in chunk.sids).lower()))
        if not words:
            return []
        approved = db.scalars(select(Chunk).where(Chunk.project_id == pid, Chunk.state == "approved",
                                                  Chunk.id != chunk.id)).all()
        scored = []
        for c in approved:
            rows = db.scalars(select(SegmentRow).where(SegmentRow.project_id == pid, SegmentRow.sid.in_(c.sids))).all()
            for r in rows:
                w2 = set(re.findall(r"\w{4,}", r.plain.lower()))
                if w2 and c.translations.get(r.sid):
                    score = len(words & w2) / len(words | w2)
                    if score > 0.15:
                        scored.append((score, {"source": r.tagged, "translation": c.translations[r.sid]}))
        scored.sort(key=lambda x: -x[0])
        return [x[1] for x in scored[:3]]

    async def _process_chunk(self, pid: int, cid: int, processor: ChunkProcessor, bible_prompt: dict,
                             chapter_summaries: dict) -> None:
        with self.store.session() as db:
            chunk = db.get(Chunk, cid)
            if chunk.state != "pending":
                return
            seg_rows = {r.sid: r for r in db.scalars(select(SegmentRow).where(
                SegmentRow.project_id == pid, SegmentRow.sid.in_(chunk.sids))).all()}
            segments = [{"id": sid, "text": seg_rows[sid].tagged} for sid in chunk.sids]
            context = self._context(db, pid, chunk, chapter_summaries)
            glossary = glossary_for(db, pid)
            run = (db.scalar(select(func.max(AttemptRow.run)).where(AttemptRow.chunk_id == cid)) or 0) + 1
        try:
            result = await processor.process(segments, context, bible_prompt, glossary)
        except BudgetExceeded:
            raise
        except Exception as exc:  # poison-chunk quarantine: one bad chunk never stops the book
            log.exception("chunk %s failed", cid)
            with self.store.session() as db:
                chunk = db.get(Chunk, cid)
                chunk.state = "needs_human"
                chunk.reasons = [f"Internal error while processing: {type(exc).__name__}. A human must translate this."]
                chunk.risk = 100
            self.store.log(pid, "system", "chunk_error", chunk=cid, error=type(exc).__name__)
            return
        with self.store.session() as db:
            chunk = db.get(Chunk, cid)
            for a in result.attempts:
                db.add(AttemptRow(chunk_id=cid, run=run, rung=a.rung, provider=a.provider, model=a.model,
                                  translations=a.translations, findings=[f.to_dict() for f in a.findings],
                                  judge=a.judge, queries=a.queries, meta=a.meta, final=a is result.final))
            if result.second_opinion is not None:
                so = result.second_opinion
                chunk.second_opinion = so.to_dict()
            chunk.translations = result.translations
            chunk.facts = result.facts
            chunk.risk = result.risk
            chunk.reasons = result.reasons + [f.message for f in result.final.findings if f.blocking][:20]
            chunk.state = result.state

    def after_translation(self, pid: int) -> None:
        src, tgt = self.languages(pid)
        with self.store.session() as db:
            p = db.get(Project, pid)
            chunks = db.scalars(select(Chunk).where(Chunk.project_id == pid)).all()
            if any(c.state == "pending" for c in chunks):
                return
            seg_text = {r.sid: r.tagged for r in db.scalars(select(SegmentRow).where(SegmentRow.project_id == pid)).all()}
            rows = [(c.id, {s: seg_text[s] for s in c.sids}, c.translations) for c in chunks if c.translations]
            flagged = audit.name_consistency(rows, src, tgt)
            for cid, fs in audit.glossary_drift(rows, glossary_for(db, pid)).items():
                flagged.setdefault(cid, []).extend(fs)
            by_id = {c.id: c for c in chunks}
            for cid, fs in flagged.items():
                c = by_id[cid]
                if c.state == "verified":
                    c.state = "needs_human"
                c.reasons = list(c.reasons or []) + [f.message for f in fs]
            if p.review_policy == "sample":
                def sentences(c: Chunk) -> int:
                    return sum(len(split_sentences(T.plain(seg_text[s]))) for s in c.sids)

                already = sum(sentences(c) for c in chunks if c.sampled)
                needed = required_sentences(self.settings.target_major_rate) - already
                if needed > 0:  # top up only; re-runs never grow an existing sample
                    items = [SampleItem(c.id, c.chapter, sentences(c)) for c in chunks
                             if c.state == "verified" and not c.sampled]
                    for cid in draw_sample(items, needed, seed=pid):
                        by_id[cid].sampled = True
            p.stage = "review"
            p.stage_detail = self._review_summary(p, chunks)
        self.store.log(pid, "system", "translation_finished")

    def _review_summary(self, p: Project, chunks: list[Chunk]) -> str:
        need = sum(1 for c in chunks if c.state == "needs_human")
        sampled = sum(1 for c in chunks if c.sampled and c.state != "approved")
        if p.review_policy == "full":
            todo = sum(1 for c in chunks if c.state != "approved")
            return f"{todo} chunk(s) need native-editor approval ({need} flagged by checks)."
        return f"{need} flagged chunk(s) and {sampled} release-sample chunk(s) to review."

    # ------------------------------------------------------------ review

    def review_queue(self, pid: int) -> list[Chunk]:
        with self.store.session() as db:
            p = db.get(Project, pid)
            chunks = db.scalars(select(Chunk).where(Chunk.project_id == pid).order_by(Chunk.chapter, Chunk.order)).all()
            if p.review_policy == "full":
                todo = [c for c in chunks if c.state in {"needs_human", "verified"}]
            else:
                todo = [c for c in chunks if c.state == "needs_human" or (c.sampled and c.state == "verified")]
            return sorted(todo, key=lambda c: (c.state != "needs_human", -c.risk, c.chapter, c.order))

    def submit_review(self, pid: int, cid: int, reviewer: str, translations: dict[str, str], *,
                      major: int = 0, critical: int = 0, seconds: float = 0.0, note: str = "",
                      glossary_rule: dict | None = None, confirm: bool = False) -> dict:
        src, tgt = self.languages(pid)
        with self.store.session() as db:
            chunk = db.get(Chunk, cid)
            if chunk is None or chunk.project_id != pid:
                raise WorkflowError("Chunk not found.")
            seg = {r.sid: r for r in db.scalars(select(SegmentRow).where(SegmentRow.project_id == pid,
                                                                         SegmentRow.sid.in_(chunk.sids))).all()}
            glossary = glossary_for(db, pid)
        new = {}
        warnings = []
        for sid in chunk.sids:
            text = gates.normalize((translations.get(sid) or "").strip())
            problems = T.validate(text, T.tag_ids(seg[sid].tagged))
            if problems:
                raise WorkflowError(f"Segment {sid}: {problems[0]}. Every formatting tag must appear exactly once.")
            if not T.plain(text).strip():
                raise WorkflowError(f"Segment {sid} is empty.")
            for f in gates.check_segment(sid, seg[sid].tagged, text, src, tgt, glossary):
                if f.blocking and f.code not in {"length", "omission", "addition"}:
                    warnings.append(f"{sid}: {f.message}")
            new[sid] = text
        if warnings and not confirm:
            return {"ok": False, "warnings": warnings}
        with self.store.session() as db:
            chunk = db.get(Chunk, cid)
            decision = "approve" if new == chunk.translations else "edit"
            db.add(Review(chunk_id=cid, reviewer=reviewer[:100], decision=decision, before=chunk.translations,
                          after=new, sample=chunk.sampled, major_errors=max(0, major),
                          critical_errors=max(0, critical), seconds=max(0.0, seconds), note=note[:2000]))
            chunk.translations = new
            chunk.state = "approved"
            chunk.human_locked = True
        self.store.log(pid, reviewer, "review", chunk=cid, decision=decision, warnings_confirmed=len(warnings))
        swept = []
        if glossary_rule and glossary_rule.get("source") and glossary_rule.get("target"):
            swept = self.add_glossary_rule(pid, glossary_rule["source"], glossary_rule["target"], reviewer)
        status = self.release_check(pid)
        return {"ok": True, "warnings": warnings, "swept": swept, "release": status}

    def add_glossary_rule(self, pid: int, source: str, target: str, actor: str) -> list[int]:
        """Error Sweep: a correction made once is enforced across the whole book."""
        source, target = source.strip(), target.strip()
        affected: list[int] = []
        with self.store.session() as db:
            db.add(GlossaryEntry(project_id=pid, source=source, target=target, note="editor rule", origin="editor"))
            seg_text = {r.sid: r.plain for r in db.scalars(select(SegmentRow).where(SegmentRow.project_id == pid)).all()}
            pattern = re.compile(rf"(?<!\w){re.escape(source.lower())}(?!\w)")
            for c in db.scalars(select(Chunk).where(Chunk.project_id == pid)).all():
                for sid in c.sids:
                    if pattern.search(seg_text.get(sid, "").lower()) and \
                            target.lower() not in T.plain(c.translations.get(sid, "")).lower():
                        affected.append(c.id)
                        if c.human_locked:
                            c.state = "needs_human"
                        else:
                            c.state = "pending"
                        c.reasons = list(c.reasons or []) + [f"New glossary rule: '{source}' must be '{target}'."]
                        break
            if affected:
                p = db.get(Project, pid)
                if any(db.get(Chunk, a).state == "pending" for a in affected):
                    p.stage, p.stage_detail = "translating", "Re-translating passages affected by a new glossary rule."
        self.store.log(pid, actor, "glossary_rule", source=source, target=target, affected=len(affected))
        return affected

    # ------------------------------------------------------------ release

    def release_check(self, pid: int) -> dict:
        with self.store.session() as db:
            p = db.get(Project, pid)
            chunks = db.scalars(select(Chunk).where(Chunk.project_id == pid)).all()
            open_ = [c for c in chunks if c.state in {"pending", "needs_human"}]
            status: dict = {"policy": p.review_policy, "releasable": False}
            if p.stage in {"bible_draft", "bible_review", "translating", "paused", "created"}:
                status["message"] = "Translation is not finished."
            elif open_:
                status["message"] = f"{len(open_)} chunk(s) still need translation or human review."
            elif all(c.state == "approved" for c in chunks):
                # A census beats a sample: every chunk was reviewed by a person.
                status.update(releasable=True, message="Every chunk was approved by a native editor.")
            elif p.review_policy == "full":
                todo = [c for c in chunks if c.state != "approved"]
                status["message"] = f"{len(todo)} chunk(s) still need native-editor approval ({p.tier} tier)."
            else:
                sampled = [c for c in chunks if c.sampled]
                unreviewed = [c for c in sampled if c.state != "approved"]
                if unreviewed:
                    status["message"] = f"{len(unreviewed)} release-sample chunk(s) still to review."
                else:
                    seg_text = {r.sid: r.plain for r in db.scalars(
                        select(SegmentRow).where(SegmentRow.project_id == pid)).all()}
                    sentences = sum(len(split_sentences(seg_text[s])) for c in sampled for s in c.sids)
                    major = critical = 0
                    failed_chapters = set()
                    for c in sampled:
                        rv = db.scalars(select(Review).where(Review.chunk_id == c.id, Review.sample)
                                        .order_by(Review.id.desc())).first()
                        if rv is not None:
                            major += rv.major_errors
                            critical += rv.critical_errors
                            if rv.major_errors or rv.critical_errors:
                                failed_chapters.add(c.chapter)
                    d = decide(sentences, major, critical, self.settings.target_major_rate)
                    status.update(releasable=d.passed, message=d.message, sentences=d.sentences, major=d.major,
                                  critical=d.critical, upper_bound=d.bound, target=d.target)
                    if not d.passed:
                        n = 0
                        for c in chunks:
                            if c.chapter in failed_chapters and c.state == "verified":
                                c.state = "needs_human"
                                c.reasons = list(c.reasons or []) + ["Release sample failed in this chapter: full review required."]
                                n += 1
                        status["message"] += f" {n} chunk(s) in the affected chapter(s) now need full review."
            if status["releasable"] and p.stage == "review":
                p.stage, p.stage_detail = "releasable", status["message"]
            elif not status["releasable"] and p.stage == "review":
                p.stage_detail = status["message"]
            p.release = status
            return status

    # ------------------------------------------------------------ export

    def export(self, pid: int, allow_draft: bool = False, render: bool = True) -> ExportResult:
        status = self.release_check(pid)
        label = "VERIFIED" if status["releasable"] else "DRAFT"
        if label == "DRAFT" and not allow_draft:
            raise WorkflowError(f"Not releasable yet: {status.get('message', '')} Export a draft instead.")
        src, tgt = self.languages(pid)
        original = self.source_bytes(pid)
        with self.store.session() as db:
            p = db.get(Project, pid)
            seg = {r.sid: r for r in db.scalars(select(SegmentRow).where(SegmentRow.project_id == pid)).all()}
            chunks = db.scalars(select(Chunk).where(Chunk.project_id == pid)).all()
            name = p.name
        translations: dict[str, str] = {}
        skipped: list[str] = []
        for c in chunks:
            for sid in c.sids:
                t = c.translations.get(sid)
                if t and not T.validate(t, T.tag_ids(seg[sid].tagged)):
                    translations[sid] = t
                else:
                    skipped.append(sid)
        if skipped and label == "VERIFIED":
            raise WorkflowError("Internal check failed: verified chunks are missing valid translations.")
        out = rebuild(original, translations, tgt)
        fidelity = self.check_fidelity(original, out, translations)
        if render:
            fidelity["source_pages"] = render_pdf_pages(original)
            fidelity["output_pages"] = render_pdf_pages(out) if fidelity["source_pages"] else None
            if fidelity["source_pages"] and not fidelity["output_pages"]:
                fidelity["problems"].append("The translated document could not be rendered by LibreOffice.")
        if fidelity["problems"]:
            raise WorkflowError("Export blocked by the fidelity gate: " + "; ".join(fidelity["problems"][:5]))
        safe = re.sub(r"[^\w.-]+", "_", name)[:80] or "document"
        path = self.project_dir(pid) / f"{safe}.{tgt.code}.{label}.docx"
        path.write_bytes(out)
        with self.store.session() as db:
            p = db.get(Project, pid)
            if label == "VERIFIED":
                p.stage = "exported"
            p.release = {**(p.release or {}), "last_export": {"label": label, "file": path.name, "fidelity": fidelity,
                                                               "skipped": len(skipped)}}
        self.store.log(pid, "system", "exported", label=label, file=path.name, skipped=len(skipped))
        return ExportResult(path, label, fidelity, skipped)

    def check_fidelity(self, original: bytes, out: bytes, translations: dict[str, str]) -> dict:
        a, b = extract(original), extract(out)
        problems: list[str] = []
        if len(a.segments) != len(b.segments):
            problems.append(f"Paragraph count changed ({len(a.segments)} -> {len(b.segments)}).")
        text_ok = fmt_ok = untouched_ok = 0
        for sa, sb in zip(a.segments, b.segments):
            if sa.sid in translations:
                want = translations[sa.sid]
                if T.plain(sb.tagged) == T.plain(want):
                    text_ok += 1
                else:
                    problems.append(f"{sa.sid}: text in the file differs from the approved translation.")
                inv_a, inv_b = formatting_inventory(sa), formatting_inventory(sb)
                if set(inv_a["g"]) == set(inv_b["g"]) and inv_a["k"] == inv_b["k"] and \
                        _flat(inv_a["x"]) == _flat(inv_b["x"]):
                    fmt_ok += 1
                else:
                    problems.append(f"{sa.sid}: formatting differs from the source.")
            else:
                if sa.tagged == sb.tagged:
                    untouched_ok += 1
                else:
                    problems.append(f"{sa.sid}: an untranslated paragraph was changed.")
        ignored = {"word/settings.xml", "word/styles.xml", *a.roots.keys()}
        for name in a.pkg.names():
            if name not in ignored and a.pkg.read(name) != b.pkg.read(name):
                problems.append(f"Package part {name} changed unexpectedly.")
        return {"problems": problems, "translated_text_matches": text_ok, "formatting_matches": fmt_ok,
                "untouched_paragraphs_identical": untouched_ok, "translated": len(translations)}


def _flat(counter) -> dict:
    """Objects counted individually, so adjacent objects merged on rebuild still compare equal."""
    out: dict[str, int] = defaultdict(int)
    for key, n in counter.items():
        for name in key.split("+"):
            out[name] += n
    return dict(out)


def prompts_version() -> str:
    from . import PROMPT_VERSION

    return PROMPT_VERSION
