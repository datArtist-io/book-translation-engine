"""Safe reading and writing of DOCX (Office Open XML) packages.

Security rules applied before any XML is parsed:
  * the file must be a real ZIP with the parts a Word document needs
  * legacy/encrypted files (OLE compound documents) are rejected with a reason
  * macro-enabled content (vbaProject.bin) is rejected
  * entry count, total uncompressed size and per-entry compression ratio are capped
    (zip-bomb protection)
  * XML is parsed with entity resolution, DTD loading and network access disabled,
    and any DOCTYPE is refused (XXE / billion-laughs protection)
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass

from lxml import etree

OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
MAX_ENTRIES = 10_000
MAX_TOTAL_UNCOMPRESSED = 2 * 1024**3  # 2 GiB
MAX_RATIO = 200


class DocxRejected(ValueError):
    """The file cannot be processed; the message is safe to show the user."""


_PARSER = etree.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    huge_tree=False,
    remove_blank_text=False,
)


def parse_xml(data: bytes) -> etree._Element:
    head = data[:2048].lower()
    if b"<!doctype" in head or b"<!entity" in data.lower():
        raise DocxRejected("The document contains a DOCTYPE or entity declaration, which is not allowed.")
    try:
        return etree.fromstring(data, parser=_PARSER)
    except etree.XMLSyntaxError as exc:
        raise DocxRejected(f"The document contains malformed XML ({exc.msg}).") from exc


def serialize_xml(root: etree._Element) -> bytes:
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


@dataclass
class _Entry:
    info: zipfile.ZipInfo
    data: bytes


class DocxPackage:
    def __init__(self, entries: list[_Entry]):
        self._entries = entries
        self._index = {e.info.filename: e for e in entries}

    @classmethod
    def from_bytes(cls, data: bytes) -> "DocxPackage":
        if data[:8] == OLE_MAGIC:
            raise DocxRejected(
                "This file is password-protected or in the old .doc format. "
                "Please remove the password or save it as .docx and try again."
            )
        if not zipfile.is_zipfile(io.BytesIO(data)):
            raise DocxRejected("This is not a valid .docx file.")
        try:
            zf = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise DocxRejected("This .docx file is corrupt.") from exc
        infos = zf.infolist()
        if len(infos) > MAX_ENTRIES:
            raise DocxRejected("The document has too many internal parts to process safely.")
        total = 0
        names = set()
        for info in infos:
            total += info.file_size
            if info.compress_size and info.file_size / max(info.compress_size, 1) > MAX_RATIO and info.file_size > 10 * 1024**2:
                raise DocxRejected("The document has an abnormal compression ratio and was rejected for safety.")
            names.add(info.filename)
        if total > MAX_TOTAL_UNCOMPRESSED:
            raise DocxRejected("The document is too large when uncompressed.")
        if "[Content_Types].xml" not in names or "word/document.xml" not in names:
            raise DocxRejected("This file is not a Word document (word/document.xml is missing).")
        if any(n.lower().endswith("vbaproject.bin") for n in names):
            raise DocxRejected("Macro-enabled documents are not accepted. Save as a plain .docx and try again.")
        entries = []
        for info in infos:
            with zf.open(info) as fh:
                chunk = fh.read(MAX_TOTAL_UNCOMPRESSED + 1)
            if len(chunk) != info.file_size:
                raise DocxRejected("The document's internal sizes do not match; it may be corrupt.")
            entries.append(_Entry(info, chunk))
        return cls(entries)

    def names(self) -> list[str]:
        return [e.info.filename for e in self._entries]

    def read(self, name: str) -> bytes:
        return self._index[name].data

    def has(self, name: str) -> bool:
        return name in self._index

    def write(self, name: str, data: bytes) -> None:
        if name in self._index:
            self._index[name].data = data
        else:
            info = zipfile.ZipInfo(name)
            info.compress_type = zipfile.ZIP_DEFLATED
            entry = _Entry(info, data)
            self._entries.append(entry)
            self._index[name] = entry

    def to_bytes(self) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as out:
            for e in self._entries:
                info = zipfile.ZipInfo(e.info.filename, date_time=e.info.date_time)
                info.compress_type = e.info.compress_type or zipfile.ZIP_DEFLATED
                info.external_attr = e.info.external_attr
                out.writestr(info, e.data)
        return buf.getvalue()
