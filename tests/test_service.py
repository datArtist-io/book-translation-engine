import asyncio

import pytest

from bte.config import Settings
from bte.docx.segments import extract
from bte.pipeline.engine import Engines
from bte.pipeline.release import decide, required_sentences, upper_bound
from bte.providers.mock import MockLLM, MockNMT
from bte.service import Service, WorkflowError
from bte.store import Chunk, Store

from .fixtures import long_body, make_docx


def make_service(tmp_path, drafter=None, **overrides):
    settings = Settings(BTE_DATA_DIR=tmp_path, BTE_DATABASE_URL=f"sqlite:///{tmp_path}/t.db", **overrides)
    engines = Engines(drafter=drafter or MockLLM("mock-a"), judge=MockLLM("mock-b"), alternate=MockLLM("mock-c"),
                      nmt=MockNMT())
    return Service(settings, Store(settings.db_url), engines)


def run_to_review(svc, data, target, name="book.docx"):
    pid = svc.create_project(data, name, "en", target)
    asyncio.run(svc.build_bible(pid))
    bible = svc.latest_bible(pid).content
    svc.approve_bible(pid, bible, "editor")
    asyncio.run(svc.translate(pid))
    return pid


def approve_all(svc, pid, major=0):
    for c in svc.review_queue(pid):
        res = svc.submit_review(pid, c.id, "ada", c.translations, major=major, confirm=True)
        assert res["ok"]


def test_full_workflow_yoruba_full_review(tmp_path):
    svc = make_service(tmp_path)
    data = make_docx()
    pid = run_to_review(svc, data, "yo")
    status = svc.release_check(pid)
    assert not status["releasable"] and "native-editor" in status["message"]
    with pytest.raises(WorkflowError, match="Not releasable"):
        svc.export(pid, render=False)
    draft = svc.export(pid, allow_draft=True, render=False)
    assert draft.label == "DRAFT" and draft.path.name.endswith(".yo.DRAFT.docx")
    approve_all(svc, pid)
    assert svc.release_check(pid)["releasable"]
    result = svc.export(pid, render=False)
    assert result.label == "VERIFIED"
    assert result.fidelity["problems"] == []
    out = extract(result.path.read_bytes())
    src = extract(data)
    para = next(s for s in out.segments if "<g1>" in s.tagged and s.part == "word/document.xml")
    assert para.tag_labels["g1"] == "bold"
    # untranslatable paragraphs untouched; headers and footnotes translated
    assert next(s for s in out.segments if s.sid == next(x.sid for x in src.segments if x.plain == "2024")).plain == "2024"
    header = next(s for s in out.segments if s.part == "word/header1.xml")
    assert header.plain != "The River and the Market"


def test_sample_policy_and_statistical_release(tmp_path):
    svc = make_service(tmp_path)
    pid = run_to_review(svc, make_docx(long_body(30)), "fr")
    queue = svc.review_queue(pid)
    assert queue and all(c.sampled or c.state == "needs_human" for c in queue)
    approve_all(svc, pid)
    status = svc.release_check(pid)
    assert status["releasable"], status


def test_failed_sample_sends_chapter_back(tmp_path):
    svc = make_service(tmp_path, BTE_TARGET_MAJOR_RATE=0.2)
    pid = run_to_review(svc, make_docx(long_body(60)), "fr")
    queue = svc.review_queue(pid)
    sampled = [c for c in queue if c.sampled]
    assert sampled and len(sampled) < len(queue) + 100
    for c in queue:
        svc.submit_review(pid, c.id, "ada", c.translations, major=12 if c.id == sampled[0].id else 0, confirm=True)
    status = svc.release_check(pid)
    assert not status["releasable"]
    with svc.store.session() as db:
        failed = [c for c in db.query(Chunk).filter_by(project_id=pid) if c.state == "needs_human"]
    assert failed or "full review" in status["message"]


def test_resume_after_crash_does_not_rebill(tmp_path):
    svc = make_service(tmp_path)
    pid = svc.create_project(make_docx(long_body(30)), "b.docx", "en", "fr")
    asyncio.run(svc.build_bible(pid))
    svc.approve_bible(pid, svc.latest_bible(pid).content, "ed")
    asyncio.run(svc.translate(pid))
    calls_first = svc.engines().drafter.calls.copy()
    # Simulate a crash that lost chunk results: mark some chunks pending again
    with svc.store.session() as db:
        for c in db.query(Chunk).filter_by(project_id=pid).limit(2):
            c.state = "pending"
    svc2 = make_service(tmp_path)
    asyncio.run(svc2.translate(pid))
    assert svc2.engines().drafter.calls == []  # every call replayed from the durable cache
    assert all(c.state != "pending" for c in svc2.review_queue(pid)) and calls_first


def test_glossary_rule_sweeps_the_book(tmp_path):
    svc = make_service(tmp_path)
    pid = run_to_review(svc, make_docx(long_body(20)), "fr")
    first = svc.review_queue(pid)[0]
    res = svc.submit_review(pid, first.id, "ada", first.translations, confirm=True,
                            glossary_rule={"source": "market", "target": "MARCHÉ"})
    assert res["ok"] and res["swept"]
    asyncio.run(svc.translate(pid))
    with svc.store.session() as db:
        for c in db.query(Chunk).filter_by(project_id=pid):
            if c.state in {"verified", "approved"} and not c.human_locked:
                joined = " ".join(c.translations.values())
                assert "MARCHÉ" in joined or "market" not in joined.lower()


def test_review_rejects_broken_tags(tmp_path):
    svc = make_service(tmp_path)
    pid = run_to_review(svc, make_docx(), "fr")
    c = next(c for c in svc.review_queue(pid) if any("<g" in t for t in c.translations.values()))
    broken = {sid: t.replace("<g1>", "").replace("</g1>", "") for sid, t in c.translations.items()}
    with pytest.raises(WorkflowError, match="tag"):
        svc.submit_review(pid, c.id, "ada", broken)


def test_review_warns_on_changed_numbers(tmp_path):
    svc = make_service(tmp_path)
    pid = run_to_review(svc, make_docx(), "fr")
    c = next(c for c in svc.review_queue(pid) if any("12" in t for t in c.translations.values()))
    edited = {sid: t.replace("12", "13") for sid, t in c.translations.items()}
    res = svc.submit_review(pid, c.id, "ada", edited)
    assert not res["ok"] and any("Numbers" in w for w in res["warnings"])


def test_rejects_bad_inputs(tmp_path):
    svc = make_service(tmp_path)
    with pytest.raises(WorkflowError):
        svc.create_project(make_docx(), "x.docx", "en", "en")
    with pytest.raises(WorkflowError):
        svc.create_project(make_docx(), "x.docx", "en", "zzzz-not-a-lang")
    with pytest.raises(WorkflowError):
        svc.create_project(b"PK", "x.pdf", "en", "fr")


def test_release_math():
    assert required_sentences(0.01) == 299
    assert abs(upper_bound(0, 299) - 0.01) < 0.0005
    assert upper_bound(2, 300) > upper_bound(0, 300)
    assert decide(300, 0, 0, 0.01).passed
    assert not decide(300, 0, 1, 0.01).passed
    assert not decide(50, 0, 0, 0.01).passed
