"""Simple web UI over the robust core: upload, approve the Book Bible, review, export.

Security: optional access token login (required off localhost), SameSite=Strict cookies,
CSRF double-submit tokens on every form, same-origin checks, strict security headers,
streamed upload size limits, and no manuscript text in logs.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import re
import secrets
import time
from collections import Counter
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup
from sqlalchemy import select
from starlette.middleware.base import BaseHTTPMiddleware

from ..docx.package import DocxRejected
from ..languages import all_languages, get_language
from ..report import report_context
from ..service import Service, WorkflowError
from ..store import AttemptRow, Chunk, Project, SegmentRow
from ..text import tags as T
from ..worker import Worker

HERE = Path(__file__).parent
TAG_RE = re.compile(r"(</?[gk]\d+>|<x\d+/>)")


def render_tagged(tagged: str, labels: dict) -> Markup:
    out = []
    for tok in T.tokenize(tagged or ""):
        if tok.kind == "text":
            out.append(html.escape(tok.value))
        elif tok.kind == "open":
            cls = "fmt" if tok.value.startswith("g") else "lnk"
            label = html.escape(labels.get(tok.value, ""))
            out.append(f'<span class="{cls}" title="{tok.value}: {label}"><sup class="tid">{tok.value}</sup>')
        elif tok.kind == "close":
            out.append("</span>")
        else:
            label = html.escape(labels.get(tok.value, ""))
            out.append(f'<span class="obj" title="{label}">[{tok.value}: {label}]</span>')
    # Safe: every text piece and label above is html.escape()d, and tag ids match [gkx]\d+.
    return Markup("".join(out))  # nosec B704


def editable(tagged: str) -> str:
    """Tagged text for a textarea: tags kept, &amp; etc. shown as plain characters."""
    parts = TAG_RE.split(tagged or "")
    return "".join(p if i % 2 else T.unescape(p) for i, p in enumerate(parts))


def from_editable(text: str) -> str:
    parts = TAG_RE.split(text or "")
    return "".join(p if i % 2 else T.escape(p) for i, p in enumerate(parts))


class SecurityHeaders(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; "
            "frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        return response


def create_app(service: Service | None = None, start_worker: bool = True) -> FastAPI:
    svc = service or Service()
    worker = Worker(svc)
    app = FastAPI(title="Book Translation Engine", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(SecurityHeaders)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals["render_tagged"] = render_tagged
    templates.env.globals["editable"] = editable
    token = svc.settings.access_token
    token_digest = hashlib.sha256(token.encode()).hexdigest() if token else ""

    if start_worker:
        @app.on_event("startup")
        def _start():  # pragma: no cover - exercised when serving
            worker.start()

    app.state.worker = worker
    app.state.service = svc

    def authed(request: Request) -> bool:
        if not token:
            return True
        return hmac.compare_digest(request.cookies.get("bte_session", ""), token_digest)

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if not path.startswith(("/static", "/login")) and not authed(request):
            return RedirectResponse("/login", status_code=303)
        if request.method == "POST":
            origin = request.headers.get("origin")
            if origin and origin.split("://", 1)[-1] != request.headers.get("host", ""):
                return HTMLResponse("Cross-origin request refused.", status_code=403)
        response = await call_next(request)
        if not request.cookies.get("bte_csrf"):
            response.set_cookie("bte_csrf", request.state.csrf if hasattr(request.state, "csrf") else secrets.token_urlsafe(24),
                                httponly=True, samesite="strict")
        return response

    def csrf_token(request: Request) -> str:
        tok = request.cookies.get("bte_csrf")
        if not tok:
            tok = secrets.token_urlsafe(24)
            request.state.csrf = tok
        return tok

    def check_csrf(request: Request, form_token: str) -> None:
        cookie = request.cookies.get("bte_csrf", "")
        if not cookie or not hmac.compare_digest(cookie, form_token or ""):
            raise HTTPException(403, "Form expired or invalid. Reload the page and try again.")

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.update(request=request, csrf=csrf_token(request), open_mode=not token)
        return templates.TemplateResponse(request, name, ctx)

    def project_or_404(db, pid: int) -> Project:
        p = db.get(Project, pid)
        if p is None:
            raise HTTPException(404, "Project not found")
        return p

    @app.exception_handler(WorkflowError)
    async def _wf(request: Request, exc: WorkflowError):
        return page(request, "error.html", message=str(exc))

    @app.exception_handler(DocxRejected)
    async def _rej(request: Request, exc: DocxRejected):
        return page(request, "error.html", message=str(exc))

    # ---------------------------------------------------------- auth

    @app.get("/login", response_class=HTMLResponse)
    def login_form(request: Request):
        return page(request, "login.html", error="")

    attempts: dict[str, list[float]] = {}

    @app.post("/login")
    def login(request: Request, access_token: str = Form(...), csrf: str = Form("")):
        check_csrf(request, csrf)
        ip = request.client.host if request.client else "?"
        now = time.monotonic()
        recent = [t for t in attempts.get(ip, []) if now - t < 600]
        if len(recent) >= 10:
            return page(request, "login.html", error="Too many attempts. Wait ten minutes and try again.")
        if token and hmac.compare_digest(access_token, token):
            attempts.pop(ip, None)
            resp = RedirectResponse("/", status_code=303)
            resp.set_cookie("bte_session", token_digest, httponly=True, samesite="strict", max_age=12 * 3600,
                            secure=request.url.scheme == "https")
            return resp
        attempts[ip] = recent + [now]
        return page(request, "login.html", error="Wrong access token.")

    # ---------------------------------------------------------- projects

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        with svc.store.session() as db:
            projects = db.scalars(select(Project).order_by(Project.id.desc())).all()
        return page(request, "index.html", projects=projects, languages=all_languages())

    @app.post("/projects")
    async def create(request: Request, file: UploadFile = File(...), source: str = Form(...), target: str = Form(...),
                     csrf: str = Form("")):
        check_csrf(request, csrf)
        limit = svc.settings.max_upload_mb * 1024 * 1024
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise WorkflowError(f"The file is larger than the {svc.settings.max_upload_mb} MB limit.")
        pid = svc.create_project(data, file.filename or "document.docx", source.strip(), target.strip())
        worker.submit(pid, "bible")
        return RedirectResponse(f"/projects/{pid}", status_code=303)

    @app.get("/projects/{pid}", response_class=HTMLResponse)
    def project(request: Request, pid: int):
        with svc.store.session() as db:
            p = project_or_404(db, pid)
            chunks = db.scalars(select(Chunk).where(Chunk.project_id == pid)).all()
            states = Counter(c.state for c in chunks)
        src, tgt = get_language(p.source_lang), get_language(p.target_lang)
        return page(request, "project.html", p=p, src=src, tgt=tgt, states=states, total=len(chunks),
                    busy=worker.busy(pid), queue=len(svc.review_queue(pid)) if p.stage in {"review", "releasable"} else 0)

    @app.post("/projects/{pid}/resume")
    def resume(request: Request, pid: int, csrf: str = Form("")):
        check_csrf(request, csrf)
        with svc.store.session() as db:
            p = project_or_404(db, pid)
            stage = p.stage
            bible_ok = svc.latest_bible(pid, approved_only=True) is not None
            if stage == "paused":
                p.stage = "translating" if bible_ok else "bible_draft"
        worker.submit(pid, "translate" if bible_ok else "bible")
        return RedirectResponse(f"/projects/{pid}", status_code=303)

    # ---------------------------------------------------------- Book Bible

    @app.get("/projects/{pid}/bible", response_class=HTMLResponse)
    def bible(request: Request, pid: int):
        with svc.store.session() as db:
            p = project_or_404(db, pid)
        b = svc.latest_bible(pid)
        if b is None:
            raise WorkflowError("The Book Bible is still being built. Refresh in a moment.")
        return page(request, "bible.html", p=p, bible=b.content, version=b.version, approved=b.approved,
                    tgt=get_language(p.target_lang))

    @app.post("/projects/{pid}/bible")
    async def approve(request: Request, pid: int):
        form = await request.form()
        check_csrf(request, str(form.get("csrf", "")))
        b = svc.latest_bible(pid)
        if b is None:
            raise WorkflowError("No Book Bible to approve yet.")
        content = dict(b.content)
        content["book_summary"] = str(form.get("book_summary", ""))[:20000]
        content["style_guide"] = str(form.get("style_guide", ""))[:20000]
        content["glossary"] = _rows(form, "g", ("source", "target", "note"))
        content["entities"] = _rows(form, "e", ("name", "target", "gender", "address", "note"))
        svc.approve_bible(pid, content, actor=str(form.get("reviewer", "editor"))[:100] or "editor")
        worker.submit(pid, "translate")
        return RedirectResponse(f"/projects/{pid}", status_code=303)

    # ---------------------------------------------------------- review

    @app.get("/projects/{pid}/review", response_class=HTMLResponse)
    def review(request: Request, pid: int, chunk: int | None = None):
        with svc.store.session() as db:
            p = project_or_404(db, pid)
        queue = svc.review_queue(pid)
        if not queue:
            return RedirectResponse(f"/projects/{pid}", status_code=303)
        current = next((c for c in queue if c.id == chunk), queue[0])
        return page(request, "review.html", **_review_ctx(svc, p, current, queue), warnings=[], drafts={})

    @app.post("/projects/{pid}/review/{cid}")
    async def submit(request: Request, pid: int, cid: int):
        form = await request.form()
        check_csrf(request, str(form.get("csrf", "")))
        with svc.store.session() as db:
            p = project_or_404(db, pid)
            chunk = db.get(Chunk, cid)
            if chunk is None or chunk.project_id != pid:
                raise HTTPException(404, "Chunk not found")
        translations = {sid: from_editable(str(form.get(f"t_{sid}", ""))) for sid in chunk.sids}
        rule = {"source": str(form.get("rule_source", "")).strip(), "target": str(form.get("rule_target", "")).strip()}
        try:
            res = svc.submit_review(
                pid, cid, str(form.get("reviewer", "editor"))[:100] or "editor", translations,
                major=_int(form.get("major")), critical=_int(form.get("critical")), seconds=_float(form.get("seconds")),
                note=str(form.get("note", "")), glossary_rule=rule if rule["source"] and rule["target"] else None,
                confirm=form.get("confirm") == "yes")
        except WorkflowError as exc:
            return page(request, "review.html", **_review_ctx(svc, p, chunk, svc.review_queue(pid)),
                        warnings=[str(exc)], drafts=translations)
        if not res["ok"]:
            return page(request, "review.html", **_review_ctx(svc, p, chunk, svc.review_queue(pid)),
                        warnings=res["warnings"], drafts=translations, need_confirm=True)
        if res.get("swept"):
            worker.submit(pid, "translate")
        return RedirectResponse(f"/projects/{pid}/review", status_code=303)

    # ---------------------------------------------------------- export and report

    @app.post("/projects/{pid}/export")
    def export(request: Request, pid: int, draft: str = Form("no"), csrf: str = Form("")):
        check_csrf(request, csrf)
        result = svc.export(pid, allow_draft=draft == "yes")
        return FileResponse(result.path, filename=result.path.name,
                            media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    @app.get("/projects/{pid}/report", response_class=HTMLResponse)
    def report(request: Request, pid: int):
        with svc.store.session() as db:
            project_or_404(db, pid)
        return page(request, "report.html", **report_context(svc, pid))

    @app.get("/healthz")
    def health():
        return {"ok": True}

    return app


def _rows(form, prefix: str, fields: tuple[str, ...]) -> list[dict]:
    rows = []
    i = 0
    while f"{prefix}_{fields[0]}_{i}" in form and i < 5000:
        row = {f: str(form.get(f"{prefix}_{f}_{i}", "")).strip()[:500] for f in fields}
        if row[fields[0]]:
            rows.append(row)
        i += 1
    return rows


def _int(v) -> int:
    try:
        return max(0, int(str(v or "0")))
    except ValueError:
        return 0


def _float(v) -> float:
    try:
        return max(0.0, min(86400.0, float(str(v or "0"))))
    except ValueError:
        return 0.0


def _review_ctx(svc: Service, p: Project, chunk: Chunk, queue: list[Chunk]) -> dict:
    with svc.store.session() as db:
        segs = {r.sid: r for r in db.scalars(select(SegmentRow).where(SegmentRow.project_id == p.id,
                                                                     SegmentRow.sid.in_(chunk.sids))).all()}
        final = db.scalars(select(AttemptRow).where(AttemptRow.chunk_id == chunk.id, AttemptRow.final)
                           .order_by(AttemptRow.id.desc())).first()
    findings = [f for f in (final.findings if final else []) if f.get("severity") in {"minor", "major", "critical"}]
    queries = final.queries if final else []
    return {
        "p": p, "chunk": chunk, "queue": queue, "segs": [segs[s] for s in chunk.sids], "findings": findings,
        "queries": queries, "attempt": final, "second": chunk.second_opinion,
        "tgt": get_language(p.target_lang), "src": get_language(p.source_lang), "need_confirm": False,
        "position": [c.id for c in queue].index(chunk.id) + 1 if chunk.id in [c.id for c in queue] else 1,
    }


app = None  # created by the CLI (bte serve) so settings load at run time
