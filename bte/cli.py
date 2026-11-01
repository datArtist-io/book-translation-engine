"""Command line: serve the web UI, translate one file, or translate a whole folder.

  bte serve [--host 127.0.0.1] [--port 8000]
  bte translate BOOK.docx --source en --target yo [--out DIR] [--approve-bible] [--draft]
  bte folder IN_DIR OUT_DIR --source en --target yo [--approve-bible] [--draft]
  bte languages [SEARCH]

Folder mode walks IN_DIR recursively, never modifies originals, mirrors the folder tree
under OUT_DIR/<target>/, and writes _manifest.csv so every input file is accounted for
(Verified, Draft, Review needed or Rejected with a reason). Re-running skips files whose
source hash already produced a result, so an interrupted run simply resumes.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import logging
import sys
from pathlib import Path

from .config import get_settings
from .docx.package import DocxRejected
from .languages import all_languages
from .service import Service, WorkflowError

MANIFEST_FIELDS = ["path", "sha256", "bytes", "status", "output", "message"]


def translate_file(svc: Service, data: bytes, name: str, source: str, target: str, approve_bible: bool,
                   draft: bool, out_dir: Path) -> tuple[str, Path | None, str]:
    pid = svc.create_project(data, name, source, target, actor="cli")
    asyncio.run(svc.build_bible(pid))
    if not approve_bible:
        return "Review needed", None, f"Book Bible awaits approval in the web UI (project {pid})."
    bible = svc.latest_bible(pid)
    svc.approve_bible(pid, bible.content, actor="cli:auto-approved")
    asyncio.run(svc.translate(pid))
    status = svc.release_check(pid)
    if not status["releasable"] and not draft:
        return "Review needed", None, f"{status.get('message', '')} Review project {pid} in the web UI, or use --draft."
    result = svc.export(pid, allow_draft=draft)
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / result.path.name
    dest.write_bytes(result.path.read_bytes())
    label = "Verified" if result.label == "VERIFIED" else "Draft"
    return label, dest, status.get("message", "")


def cmd_translate(args) -> int:
    svc = Service()
    path = Path(args.file)
    try:
        status, out, msg = translate_file(svc, path.read_bytes(), path.name, args.source, args.target,
                                          args.approve_bible, args.draft, Path(args.out or path.parent))
    except (WorkflowError, DocxRejected) as exc:
        print(f"Rejected: {exc}", file=sys.stderr)
        return 2
    print(f"{status}: {out or '-'}\n{msg}")
    return 0


def _load_manifest(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    with path.open(newline="", encoding="utf-8") as fh:
        return {row["path"]: row for row in csv.DictReader(fh)}


def _write_manifest(path: Path, rows: dict[str, dict]) -> None:
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=MANIFEST_FIELDS)
        w.writeheader()
        for row in sorted(rows.values(), key=lambda r: r["path"]):
            w.writerow({k: row.get(k, "") for k in MANIFEST_FIELDS})
    tmp.replace(path)  # atomic: a crash never leaves a half-written manifest


def cmd_folder(args) -> int:
    src_root = Path(args.input).resolve()
    out_root = (Path(args.output).resolve() / args.target)
    out_root.mkdir(parents=True, exist_ok=True)
    manifest_path = out_root / "_manifest.csv"
    rows = _load_manifest(manifest_path)
    svc = Service()
    files = sorted(p for p in src_root.rglob("*") if p.is_file())
    done = {"Verified", "Draft"}
    for f in files:
        rel = f.relative_to(src_root).as_posix()
        if f.name.startswith(("~$", ".")) or any(part.startswith(".") for part in f.relative_to(src_root).parts):
            rows[rel] = {"path": rel, "status": "Skipped", "message": "Hidden or temporary file."}
            continue
        data = f.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        prev = rows.get(rel)
        if prev and prev.get("sha256") == digest and prev.get("status") in done:
            continue  # already translated, unchanged
        row = {"path": rel, "sha256": digest, "bytes": str(len(data))}
        if f.suffix.lower() != ".docx":
            row.update(status="Rejected", message="Format not supported in this version (only .docx).")
        else:
            try:
                status, out, msg = translate_file(svc, data, f.name, args.source, args.target, args.approve_bible,
                                                  args.draft, out_root / f.relative_to(src_root).parent)
                row.update(status=status, output=str(out.relative_to(out_root)) if out else "", message=msg)
            except (WorkflowError, DocxRejected) as exc:
                row.update(status="Rejected", message=str(exc))
            except Exception as exc:  # never lose track of a file
                row.update(status="Failed", message=f"{type(exc).__name__}: rerun to resume.")
        rows[rel] = row
        _write_manifest(manifest_path, rows)
        print(f"{row['status']:>13}  {rel}")
    _write_manifest(manifest_path, rows)
    counts: dict[str, int] = {}
    for r in rows.values():
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("Summary: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items())))
    print(f"Manifest: {manifest_path}")
    return 0


def cmd_serve(args) -> int:
    import uvicorn

    from .web.app import create_app

    settings = get_settings()
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not settings.access_token:
        print("Refusing to serve beyond localhost without BTE_ACCESS_TOKEN set.", file=sys.stderr)
        return 2
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="info")
    return 0


def cmd_languages(args) -> int:
    q = (args.search or "").lower()
    for code, name in all_languages():
        if not q or q in name.lower() or q == code:
            print(f"{code}\t{name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="bte", description="Accuracy-first book translation engine")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("serve", help="run the web UI")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(fn=cmd_serve)

    for name, fn, help_ in [("translate", cmd_translate, "translate one .docx"),
                            ("folder", cmd_folder, "translate every file in a folder")]:
        p = sub.add_parser(name, help=help_)
        if name == "translate":
            p.add_argument("file")
            p.add_argument("--out")
        else:
            p.add_argument("input")
            p.add_argument("output")
        p.add_argument("--source", required=True)
        p.add_argument("--target", required=True)
        p.add_argument("--approve-bible", action="store_true",
                       help="auto-approve the generated Book Bible (skips the human check of names and terms)")
        p.add_argument("--draft", action="store_true", help="export even if human review is still required")
        p.set_defaults(fn=fn)

    p = sub.add_parser("languages", help="list supported languages")
    p.add_argument("search", nargs="?")
    p.set_defaults(fn=cmd_languages)

    args = parser.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
