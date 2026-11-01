"""QA report data: what was checked, what was found, who approved, and how accurate the book is."""

from __future__ import annotations

from collections import Counter

from sqlalchemy import select

from .languages import get_language
from .service import Service
from .store import AttemptRow, Bible, Chunk, Project, Review


def report_context(svc: Service, pid: int) -> dict:
    with svc.store.session() as db:
        p = db.get(Project, pid)
        chunks = db.scalars(select(Chunk).where(Chunk.project_id == pid).order_by(Chunk.chapter, Chunk.order)).all()
        finals = db.scalars(select(AttemptRow).join(Chunk, AttemptRow.chunk_id == Chunk.id)
                            .where(Chunk.project_id == pid, AttemptRow.final)).all()
        all_attempts = db.scalars(select(AttemptRow).join(Chunk, AttemptRow.chunk_id == Chunk.id)
                                  .where(Chunk.project_id == pid)).all()
        reviews = db.scalars(select(Review).join(Chunk, Review.chunk_id == Chunk.id)
                             .where(Chunk.project_id == pid)).all()
        bible = db.scalars(select(Bible).where(Bible.project_id == pid, Bible.approved)
                           .order_by(Bible.version.desc())).first()
        src, tgt = get_language(p.source_lang), get_language(p.target_lang)
        states = Counter(c.state for c in chunks)
        found = Counter()
        for a in all_attempts:
            for f in a.findings:
                if f.get("severity") in {"major", "critical"}:
                    found[(f.get("layer"), f.get("code"), f.get("severity"))] += 1
        rungs = Counter(a.rung for a in finals)
        judged = [a for a in finals if a.judge and isinstance(a.judge.get("score"), (int, float))]
        independent = sum(1 for a in finals if not any(f.get("code") == "judge_same_family" for f in a.findings))
        tte = [r.seconds for r in reviews if r.seconds > 0]
        return {
            "project": p,
            "src": src,
            "tgt": tgt,
            "states": dict(states),
            "chunks_total": len(chunks),
            "findings": sorted(found.items(), key=lambda kv: (-kv[1], kv[0])),
            "rungs": dict(rungs),
            "avg_judge": round(sum(a.judge["score"] for a in judged) / len(judged), 1) if judged else None,
            "independent_judge_share": (independent / len(finals)) if finals else None,
            "reviews": len(reviews),
            "edits": sum(1 for r in reviews if r.decision == "edit"),
            "reviewers": sorted({r.reviewer for r in reviews}),
            "avg_tte": round(sum(tte) / len(tte), 1) if tte else None,
            "bible_version": bible.version if bible else None,
            "bible_approved_by": bible.approved_by if bible else None,
            "flagged": [c for c in chunks if c.state == "needs_human"],
            "release": p.release or {},
            "usage": p.usage or {},
            "models": p.models or {},
            "warnings": p.warnings or [],
            "layers": [
                ("L0 hard gates", "Tags, numbers, links, glossary, script, untranslated text, length, repetition, orthography", "on"),
                ("L1 translation-or-answer", "Heuristics on every segment plus the judge's verdict", "on"),
                ("L2 coverage alignment", "Gale-Church sentence alignment for omissions and additions", "on"),
                ("L4 learned quality estimation", "SSA-COMET-QE / CometKiwi not yet wired in this version", "planned"),
                ("L5 independent judge and fact check", "Different model family; facts extracted then confirmed", "on"),
                ("L6 book-level audit", "Name and glossary consistency across the book", "on"),
                ("L7 human gate", f"{'Full native review' if p.review_policy == 'full' else 'Statistical release sample'}", "on"),
            ],
        }
