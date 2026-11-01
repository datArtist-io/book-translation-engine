import csv
import re

from fastapi.testclient import TestClient

from bte.cli import main
from bte.config import Settings, reset_settings_cache
from bte.pipeline.engine import Engines
from bte.providers.mock import MockLLM, MockNMT
from bte.service import Service
from bte.store import Store
from bte.web.app import create_app, editable, from_editable

from .fixtures import make_docx


def svc_for(tmp_path, **kw):
    s = Settings(BTE_DATA_DIR=tmp_path, BTE_DATABASE_URL=f"sqlite:///{tmp_path}/w.db", **kw)
    return Service(s, Store(s.db_url), Engines(MockLLM("mock-a"), MockLLM("mock-b"), MockLLM("mock-c"), MockNMT()))


def csrf(client):
    client.get("/")
    return client.cookies.get("bte_csrf")


def test_web_flow_with_security(tmp_path):
    svc = svc_for(tmp_path)
    app = create_app(svc, start_worker=False)
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200 and "New translation" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
    token = client.cookies.get("bte_csrf")
    # missing CSRF token is refused
    r = client.post("/projects", files={"file": ("b.docx", make_docx())}, data={"source": "en", "target": "fr"})
    assert r.status_code == 403
    # cross-origin post refused
    r = client.post("/projects", files={"file": ("b.docx", make_docx())},
                    data={"source": "en", "target": "fr", "csrf": token}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/projects", files={"file": ("b.docx", make_docx())},
                    data={"source": "en", "target": "fr", "csrf": token}, follow_redirects=False)
    assert r.status_code == 303
    pid = int(r.headers["location"].rsplit("/", 1)[1])
    import asyncio
    asyncio.run(svc.build_bible(pid))
    r = client.get(f"/projects/{pid}/bible")
    assert "Book Bible" in r.text
    r = client.post(f"/projects/{pid}/bible", data={"csrf": token, "book_summary": "s", "style_guide": "g",
                                                    "g_source_0": "market", "g_target_0": "marché", "reviewer": "ada"},
                    follow_redirects=False)
    assert r.status_code == 303
    asyncio.run(svc.translate(pid))
    r = client.get(f"/projects/{pid}/review")
    assert r.status_code == 200 and "textarea" in r.text
    # submit every chunk in the queue
    while True:
        queue = svc.review_queue(pid)
        if not queue:
            break
        c = queue[0]
        data = {"csrf": token, "reviewer": "ada", "confirm": "yes"}
        data.update({f"t_{sid}": editable(t) for sid, t in c.translations.items()})
        r = client.post(f"/projects/{pid}/review/{c.id}", data=data, follow_redirects=False)
        assert r.status_code == 303, r.text[:500]
    r = client.post(f"/projects/{pid}/export", data={"csrf": token})
    assert r.status_code == 200 and r.content[:2] == b"PK"
    r = client.get(f"/projects/{pid}/report")
    assert "VERIFIED" in r.text and "Fidelity gate" in r.text


def test_access_token_required(tmp_path):
    svc = svc_for(tmp_path, BTE_ACCESS_TOKEN="s3cret-token")
    client = TestClient(create_app(svc, start_worker=False))
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    client.get("/login")
    tok = client.cookies.get("bte_csrf")
    r = client.post("/login", data={"access_token": "wrong", "csrf": tok})
    assert "Wrong access token" in r.text
    r = client.post("/login", data={"access_token": "s3cret-token", "csrf": tok}, follow_redirects=False)
    assert r.status_code == 303
    assert client.get("/").status_code == 200


def test_editable_round_trip():
    tagged = "Tom &amp; Ada <g1>sold</g1> &lt;5&gt;<x2/>"
    assert editable(tagged) == "Tom & Ada <g1>sold</g1> <5><x2/>"
    assert from_editable(editable(tagged)) == tagged


def test_folder_mode_mirrors_tree_and_manifest(tmp_path, monkeypatch):
    src = tmp_path / "in"
    (src / "Series A").mkdir(parents=True)
    original = make_docx()
    (src / "Series A" / "book1.docx").write_bytes(original)
    (src / "notes.pdf").write_bytes(b"%PDF-1.4")
    (src / "~$lock.docx").write_bytes(b"x")
    out = tmp_path / "out"
    monkeypatch.setenv("BTE_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("BTE_DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")
    for k in ("BTE_DRAFTER", "BTE_JUDGE", "BTE_ALTERNATE"):
        monkeypatch.setenv(k, "mock:" + k[-1].lower())
    monkeypatch.setenv("BTE_NMT", "mock")
    reset_settings_cache()
    assert main(["folder", str(src), str(out), "--source", "en", "--target", "fr", "--approve-bible", "--draft"]) == 0
    manifest = list(csv.DictReader((out / "fr" / "_manifest.csv").open()))
    by = {r["path"]: r for r in manifest}
    assert by["Series A/book1.docx"]["status"] in {"Verified", "Draft"}
    assert (out / "fr" / by["Series A/book1.docx"]["output"]).exists()
    assert by["notes.pdf"]["status"] == "Rejected"
    assert by["~$lock.docx"]["status"] == "Skipped"
    # originals untouched
    assert (src / "Series A" / "book1.docx").read_bytes() == original
    # re-run resumes: nothing re-translated
    assert main(["folder", str(src), str(out), "--source", "en", "--target", "fr", "--approve-bible", "--draft"]) == 0
    reset_settings_cache()


def test_login_rate_limited(tmp_path):
    svc = svc_for(tmp_path, BTE_ACCESS_TOKEN="s3cret-token")
    client = TestClient(create_app(svc, start_worker=False))
    client.get("/login")
    tok = client.cookies.get("bte_csrf")
    for _ in range(10):
        client.post("/login", data={"access_token": "guess", "csrf": tok})
    r = client.post("/login", data={"access_token": "s3cret-token", "csrf": tok})
    assert "Too many attempts" in r.text
