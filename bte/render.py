"""Optional render check: prove a DOCX opens and lays out, using LibreOffice headless."""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
from pathlib import Path


def soffice() -> str | None:
    return shutil.which("soffice") or shutil.which("libreoffice")


def render_pdf_pages(docx: bytes, timeout: int = 240) -> int | None:
    """Return the page count of the rendered PDF, or None if rendering is unavailable/failed."""
    exe = soffice()
    if not exe:
        return None
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "doc.docx"
        src.write_bytes(docx)
        profile = Path(tmp) / "profile"
        try:
            subprocess.run(
                [exe, f"-env:UserInstallation=file://{profile}", "--headless", "--norestore",
                 "--convert-to", "pdf", "--outdir", tmp, str(src)],
                check=True, capture_output=True, timeout=timeout,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        pdf = Path(tmp) / "doc.pdf"
        if not pdf.exists():
            return None
        data = pdf.read_bytes()
        return len(re.findall(rb"/Type\s*/Page(?!s)", data)) or None
