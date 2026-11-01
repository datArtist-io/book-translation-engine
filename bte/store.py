"""Persistent state. Postgres in production, SQLite by default.

Nothing is overwritten: every translation attempt, finding, human review and event is
a new row, so any word in the output can be traced back to its source and its checks.
The database is the single source of truth for job progress; a crashed or restarted
worker resumes from it.
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import contextmanager
from typing import Any, Iterator

from sqlalchemy import (JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event,
                        select)
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker


def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class Base(DeclarativeBase):
    pass


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(300))
    filename: Mapped[str] = mapped_column(String(300))
    source_lang: Mapped[str] = mapped_column(String(35))
    target_lang: Mapped[str] = mapped_column(String(35))
    tier: Mapped[str] = mapped_column(String(4))
    review_policy: Mapped[str] = mapped_column(String(10))
    # created -> bible_draft -> bible_review -> translating -> review -> releasable -> exported | paused | failed
    stage: Mapped[str] = mapped_column(String(30), default="created")
    stage_detail: Mapped[str] = mapped_column(Text, default="")
    source_sha256: Mapped[str] = mapped_column(String(64))
    warnings: Mapped[list] = mapped_column(JSON, default=list)
    models: Mapped[dict] = mapped_column(JSON, default=dict)  # pinned for the whole book
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    release: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class SegmentRow(Base):
    __tablename__ = "segments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    sid: Mapped[str] = mapped_column(String(20))
    part: Mapped[str] = mapped_column(String(100))
    ordinal: Mapped[int] = mapped_column(Integer)
    tagged: Mapped[str] = mapped_column(Text)
    plain: Mapped[str] = mapped_column(Text)
    translatable: Mapped[bool] = mapped_column(Boolean)
    style: Mapped[str] = mapped_column(String(100), default="")
    heading_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tag_labels: Mapped[dict] = mapped_column(JSON, default=dict)
    tag_keys: Mapped[dict] = mapped_column(JSON, default=dict)
    chapter: Mapped[int] = mapped_column(Integer, default=0)
    chunk_id: Mapped[int | None] = mapped_column(ForeignKey("chunks.id"), nullable=True, index=True)


class Chunk(Base):
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    chapter: Mapped[int] = mapped_column(Integer)
    order: Mapped[int] = mapped_column(Integer)
    sids: Mapped[list] = mapped_column(JSON)
    source_hash: Mapped[str] = mapped_column(String(64))
    # pending -> verified | needs_human -> approved (human) ; stale re-enters pending
    state: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    translations: Mapped[dict] = mapped_column(JSON, default=dict)
    facts: Mapped[list] = mapped_column(JSON, default=list)
    risk: Mapped[int] = mapped_column(Integer, default=0)
    reasons: Mapped[list] = mapped_column(JSON, default=list)
    second_opinion: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    human_locked: Mapped[bool] = mapped_column(Boolean, default=False)
    sampled: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AttemptRow(Base):
    __tablename__ = "attempts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id"), index=True)
    run: Mapped[int] = mapped_column(Integer, default=1)
    rung: Mapped[str] = mapped_column(String(30))
    provider: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(100))
    translations: Mapped[dict] = mapped_column(JSON)
    findings: Mapped[list] = mapped_column(JSON)
    judge: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    queries: Mapped[list] = mapped_column(JSON, default=list)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    final: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class GlossaryEntry(Base):
    __tablename__ = "glossary"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    source: Mapped[str] = mapped_column(String(300))
    target: Mapped[str] = mapped_column(String(300))
    note: Mapped[str] = mapped_column(Text, default="")
    origin: Mapped[str] = mapped_column(String(30), default="bible")  # bible | editor | sweep


class Bible(Base):
    __tablename__ = "bibles"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)
    approved: Mapped[bool] = mapped_column(Boolean, default=False)
    approved_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Review(Base):
    __tablename__ = "reviews"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chunk_id: Mapped[int] = mapped_column(ForeignKey("chunks.id"), index=True)
    reviewer: Mapped[str] = mapped_column(String(100))
    decision: Mapped[str] = mapped_column(String(20))  # approve | edit | reject
    before: Mapped[dict] = mapped_column(JSON)
    after: Mapped[dict] = mapped_column(JSON)
    sample: Mapped[bool] = mapped_column(Boolean, default=False)
    major_errors: Mapped[int] = mapped_column(Integer, default=0)
    critical_errors: Mapped[int] = mapped_column(Integer, default=0)
    seconds: Mapped[float] = mapped_column(Float, default=0.0)  # Time to Edit
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class CacheRow(Base):
    __tablename__ = "llm_cache"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class EventRow(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    project_id: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(60))
    data: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), default=now)


class Store:
    def __init__(self, url: str):
        kwargs: dict[str, Any] = {}
        if url.startswith("sqlite"):
            kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
        self.engine = create_engine(url, future=True, **kwargs)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _pragmas(dbapi_conn, _):  # pragma: no cover - trivial
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.close()
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(self.engine, expire_on_commit=False, future=True)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.Session()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def log(self, project_id: int | None, actor: str, action: str, **data) -> None:
        with self.session() as s:
            s.add(EventRow(project_id=project_id, actor=actor, action=action, data=data))


class DbCache:
    """Response cache backed by the database, so retries after a crash never re-bill."""

    def __init__(self, store: Store):
        self.store = store

    def get(self, key: str) -> dict | None:
        with self.store.session() as s:
            row = s.get(CacheRow, key)
            return json.loads(row.value) if row else None

    def put(self, key: str, value: dict) -> None:
        with self.store.session() as s:
            if s.get(CacheRow, key) is None:
                s.add(CacheRow(key=key, value=json.dumps(value, ensure_ascii=False)))


def glossary_for(s: Session, project_id: int) -> list[dict]:
    rows = s.scalars(select(GlossaryEntry).where(GlossaryEntry.project_id == project_id)).all()
    return [{"source": r.source, "target": r.target, "note": r.note} for r in rows]
