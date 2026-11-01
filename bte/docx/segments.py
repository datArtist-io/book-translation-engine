"""Extract translatable paragraphs from DOCX as tagged text, and rebuild them.

Extraction is deterministic: running it twice on the same file yields the same
segment ids, tag ids and tagged text. Rebuild re-extracts the original to get live
element handles, then replaces only the words of each paragraph, leaving paragraph
properties, styles, images, fields, numbering, sections and every other part of the
package untouched.
"""

from __future__ import annotations

import copy
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field

from lxml import etree

from ..languages import LanguageInfo
from ..text import tags as T
from .package import DocxPackage, parse_xml, serialize_xml

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"


def w(tag: str) -> str:
    return f"{{{W}}}{tag}"


def local(el: etree._Element) -> str:
    tag = el.tag
    if not isinstance(tag, str):  # comments / processing instructions
        return ""
    return tag.rsplit("}", 1)[-1]


WRAPPERS = {"hyperlink", "smartTag", "customXml", "fldSimple", "sdt"}
UNWRAP = {"ins", "moveTo"}  # tracked insertions are accepted
DROP_CONTAINERS = {"del", "moveFrom"}  # tracked deletions are removed
DROP_INLINE = {"proofErr", "lastRenderedPageBreak", "delText", "rsid"}
SKIP_FOOTNOTE_TYPES = {"separator", "continuationSeparator", "continuationNotice"}

CJK_SCRIPTS = {"Hans", "Hant", "Jpan", "Kore", "Hang"}


def rpr_key(rpr: etree._Element | None) -> str:
    """Canonical key for run formatting, ignoring revision ids and language tags."""
    if rpr is None:
        return ""
    clone = copy.deepcopy(rpr)
    for el in clone.iter():
        for attr in list(el.attrib):
            if "rsid" in attr:
                del el.attrib[attr]
    for child in list(clone):
        if local(child) in {"lang", "noProof", "rPrChange"}:
            clone.remove(child)
    return etree.tostring(clone, method="c14n").decode()


def rpr_label(rpr: etree._Element | None) -> str:
    """Short human description of a formatting span, for the review UI and reports."""
    if rpr is None:
        return "plain"
    parts = []
    names = {local(c): c for c in rpr}
    for key, label in [("b", "bold"), ("i", "italic"), ("u", "underline"), ("strike", "strikethrough"),
                       ("smallCaps", "small caps"), ("caps", "all caps"), ("highlight", "highlight")]:
        el = names.get(key)
        if el is not None and el.get(w("val"), "true") not in {"0", "false", "none"}:
            parts.append(label)
    va = names.get("vertAlign")
    if va is not None:
        parts.append(va.get(w("val"), ""))
    if "color" in names:
        parts.append(f"colour {names['color'].get(w('val'))}")
    if "sz" in names:
        try:
            parts.append(f"{int(names['sz'].get(w('val'))) / 2:g}pt")
        except (TypeError, ValueError):
            pass
    if "rFonts" in names:
        f = names["rFonts"].get(w("ascii")) or names["rFonts"].get(w("hAnsi"))
        if f:
            parts.append(f)
    if "rStyle" in names:
        parts.append(f"style {names['rStyle'].get(w('val'))}")
    return ", ".join(parts) or "formatted"


@dataclass
class TagSpec:
    kind: str  # "g" | "k" | "x"
    label: str
    rpr: etree._Element | None = None  # g: run properties
    shell: etree._Element | None = None  # k: wrapper element without content
    content_path: str | None = None  # k: "self" or "sdtContent"
    objects: list[tuple[etree._Element, etree._Element | None, str]] = field(default_factory=list)
    # x: (element, run properties, level "run"|"para")
    key: str = ""  # g: rpr key; k: wrapper type; x: element names


@dataclass
class Segment:
    sid: str
    part: str
    ordinal: int
    tagged: str
    plain: str
    translatable: bool
    style: str = ""
    heading_level: int | None = None
    tag_labels: dict[str, str] = field(default_factory=dict)
    tag_keys: dict[str, str] = field(default_factory=dict)


@dataclass
class _Handle:
    p: etree._Element
    base_rpr: etree._Element | None
    specs: dict[str, TagSpec]


@dataclass
class _Item:
    kind: str  # "text" | "opaque"
    chain: tuple[str, ...]
    text: str = ""
    rpr: etree._Element | None = None
    obj: etree._Element | None = None
    level: str = "run"


class _ParagraphReader:
    def __init__(self, p: etree._Element):
        self.p = p
        self.items: list[_Item] = []
        self.wrappers: dict[str, tuple[etree._Element, str, str]] = {}
        self._wcount = 0
        self.dropped_revisions = 0

    def read(self) -> None:
        self._walk(self.p, ())

    def _new_wrapper(self, el: etree._Element) -> str:
        self._wcount += 1
        wid = f"w{self._wcount}"
        name = local(el)
        shell = copy.deepcopy(el)
        if name == "sdt":
            content = shell.find(w("sdtContent"))
            path = "sdtContent"
        else:
            content = shell
            path = "self"
        if content is not None:
            for child in list(content):
                if not local(child).endswith("Pr"):
                    content.remove(child)
        self.wrappers[wid] = (shell, path, name)
        return wid

    def _walk(self, container: etree._Element, chain: tuple[str, ...]) -> None:
        for child in container:
            name = local(child)
            if not name or name == "pPr":
                continue
            if name == "r":
                self._run(child, chain)
            elif name in WRAPPERS:
                wid = self._new_wrapper(child)
                inner = child.find(w("sdtContent")) if name == "sdt" else child
                if inner is not None:
                    self._walk(inner, chain + (wid,))
            elif name in UNWRAP:
                self._walk(child, chain)
            elif name in DROP_CONTAINERS:
                self.dropped_revisions += 1
            elif name in DROP_INLINE:
                continue
            else:
                self.items.append(_Item("opaque", chain, obj=child, level="para"))

    def _run(self, r: etree._Element, chain: tuple[str, ...]) -> None:
        rpr = r.find(w("rPr"))
        for child in r:
            name = local(child)
            if not name or name == "rPr" or name in DROP_INLINE:
                continue
            if name == "t":
                self.items.append(_Item("text", chain, text=child.text or "", rpr=rpr))
            elif name == "noBreakHyphen":
                self.items.append(_Item("text", chain, text="‑", rpr=rpr))
            elif name == "softHyphen":
                continue
            else:
                self.items.append(_Item("opaque", chain, rpr=rpr, obj=child, level="run"))


def _build_tagged(reader: _ParagraphReader) -> tuple[str, dict[str, TagSpec], etree._Element | None]:
    # Base formatting = the run properties covering the most characters.
    weights: Counter = Counter()
    rprs: dict[str, etree._Element | None] = {}
    for it in reader.items:
        if it.kind == "text":
            k = rpr_key(it.rpr)
            weights[k] += len(it.text)
            rprs.setdefault(k, it.rpr)
    base_key = weights.most_common(1)[0][0] if weights else ""
    base_rpr = rprs.get(base_key)

    out: list[str] = []
    specs: dict[str, TagSpec] = {}
    counter = 0
    open_chain: list[str] = []  # wrapper ids currently open
    wrapper_tag: dict[str, str] = {}  # wrapper id -> tag id
    open_g: str | None = None
    open_g_key: str | None = None
    pending_x: TagSpec | None = None

    def next_id(kind: str) -> str:
        nonlocal counter
        counter += 1
        return f"{kind}{counter}"

    def close_g() -> None:
        nonlocal open_g, open_g_key
        if open_g:
            out.append(f"</{open_g}>")
            open_g, open_g_key = None, None

    def flush_x() -> None:
        nonlocal pending_x
        pending_x = None

    def set_chain(chain: tuple[str, ...]) -> None:
        # Close wrappers not in the new chain, open new ones.
        common = 0
        while common < len(open_chain) and common < len(chain) and open_chain[common] == chain[common]:
            common += 1
        if common < len(open_chain):
            close_g()
            flush_x()
            for wid in reversed(open_chain[common:]):
                out.append(f"</{wrapper_tag[wid]}>")
            del open_chain[common:]
        for wid in chain[common:]:
            close_g()
            flush_x()
            tid = next_id("k")
            shell, path, wname = reader.wrappers[wid]
            specs[tid] = TagSpec("k", label=_wrapper_label(shell, wname), shell=shell, content_path=path, key=wname)
            wrapper_tag[wid] = tid
            out.append(f"<{tid}>")
            open_chain.append(wid)

    for it in reader.items:
        set_chain(it.chain)
        if it.kind == "text":
            flush_x()
            if not it.text:
                continue
            k = rpr_key(it.rpr)
            if k == base_key:
                close_g()
            elif open_g_key != k:
                close_g()
                tid = next_id("g")
                specs[tid] = TagSpec("g", label=rpr_label(it.rpr), rpr=it.rpr, key=k)
                out.append(f"<{tid}>")
                open_g, open_g_key = tid, k
            out.append(T.escape(it.text))
        else:
            if pending_x is None:
                close_g()
                tid = next_id("x")
                pending_x = TagSpec("x", label="", key="")
                specs[tid] = pending_x
                out.append(f"<{tid}/>")
            pending_x.objects.append((it.obj, it.rpr, it.level))
    set_chain(())
    close_g()
    for spec in specs.values():
        if spec.kind == "x":
            names = [local(o) for o, _, _ in spec.objects]
            spec.key = "+".join(names)
            spec.label = _object_label(names)
    return "".join(out), specs, base_rpr


def _wrapper_label(shell: etree._Element, name: str) -> str:
    if name == "hyperlink":
        anchor = shell.get(w("anchor"))
        return f"link to #{anchor}" if anchor else "hyperlink"
    return {"sdt": "content control", "fldSimple": "field", "smartTag": "smart tag", "customXml": "custom XML"}.get(name, name)


_OBJ_LABELS = {
    "tab": "tab", "br": "line break", "cr": "line break", "drawing": "image or shape", "pict": "image",
    "object": "embedded object", "footnoteReference": "footnote marker", "endnoteReference": "endnote marker",
    "commentReference": "comment marker", "fldChar": "field", "instrText": "field code", "sym": "symbol",
    "bookmarkStart": "bookmark", "bookmarkEnd": "bookmark", "footnoteRef": "footnote number", "endnoteRef": "endnote number",
}


def _object_label(names: list[str]) -> str:
    labels = []
    for n in names:
        lab = _OBJ_LABELS.get(n, n)
        if lab not in labels:
            labels.append(lab)
    return ", ".join(labels)


def _translatable(text: str) -> bool:
    return any(unicodedata.category(ch).startswith("L") for ch in text)


def _style_map(pkg: DocxPackage) -> dict[str, str]:
    if not pkg.has("word/styles.xml"):
        return {}
    root = parse_xml(pkg.read("word/styles.xml"))
    out = {}
    for st in root.iter(w("style")):
        sid = st.get(w("styleId"))
        name_el = st.find(w("name"))
        if sid and name_el is not None:
            out[sid] = (name_el.get(w("val")) or "").lower()
    return out


_HEADING_RE = re.compile(r"^heading\s*(\d)$")


def _heading_level(p: etree._Element, styles: dict[str, str]) -> tuple[str, int | None]:
    ppr = p.find(w("pPr"))
    style_id = ""
    if ppr is not None:
        ps = ppr.find(w("pStyle"))
        if ps is not None:
            style_id = ps.get(w("val")) or ""
        ol = ppr.find(w("outlineLvl"))
        if ol is not None:
            try:
                return style_id, int(ol.get(w("val"))) + 1
            except (TypeError, ValueError):
                pass
    name = styles.get(style_id, style_id.lower())
    if name == "title":
        return style_id, 0
    m = _HEADING_RE.match(name)
    if m:
        return style_id, int(m.group(1))
    return style_id, None


def translatable_parts(pkg: DocxPackage) -> list[str]:
    names = pkg.names()
    order = ["word/document.xml", "word/footnotes.xml", "word/endnotes.xml"]
    parts = [n for n in order if n in names]
    parts += sorted(n for n in names if re.match(r"^word/(header|footer)\d*\.xml$", n))
    return parts


def _paragraphs(root: etree._Element, part: str) -> list[etree._Element]:
    paras = []
    for p in root.iter(w("p")):
        if part.endswith(("footnotes.xml", "endnotes.xml")):
            note = p.getparent()
            while note is not None and local(note) not in {"footnote", "endnote"}:
                note = note.getparent()
            if note is not None and note.get(w("type")) in SKIP_FOOTNOTE_TYPES:
                continue
        paras.append(p)
    return paras


@dataclass
class Extraction:
    pkg: DocxPackage
    roots: dict[str, etree._Element]
    segments: list[Segment]
    handles: dict[str, _Handle]
    warnings: list[str]


def extract(data: bytes) -> Extraction:
    pkg = DocxPackage.from_bytes(data)
    styles = _style_map(pkg)
    roots: dict[str, etree._Element] = {}
    segments: list[Segment] = []
    handles: dict[str, _Handle] = {}
    warnings: list[str] = []
    dropped = 0
    n = 0
    for part in translatable_parts(pkg):
        root = parse_xml(pkg.read(part))
        roots[part] = root
        for ordinal, p in enumerate(_paragraphs(root, part)):
            reader = _ParagraphReader(p)
            reader.read()
            dropped += reader.dropped_revisions
            tagged, specs, base = _build_tagged(reader)
            n += 1
            sid = f"s{n}"
            plain_text = T.plain(tagged)
            style, level = _heading_level(p, styles) if part == "word/document.xml" else ("", None)
            segments.append(
                Segment(
                    sid=sid, part=part, ordinal=ordinal, tagged=tagged, plain=plain_text,
                    translatable=_translatable(plain_text), style=style, heading_level=level,
                    tag_labels={k: v.label for k, v in specs.items()},
                    tag_keys={k: v.key for k, v in specs.items()},
                )
            )
            handles[sid] = _Handle(p, base, specs)
    if dropped:
        warnings.append(f"{dropped} tracked deletion(s) were removed and tracked insertions accepted.")
    graphic_text = _count_graphic_text(pkg)
    if graphic_text:
        warnings.append(
            f"{graphic_text} text item(s) inside charts, diagrams or drawing shapes are not translated "
            "in this version and are flagged for a designer."
        )
    if pkg.has("word/comments.xml"):
        warnings.append("Comments are kept in the source language.")
    return Extraction(pkg, roots, segments, handles, warnings)


def _count_graphic_text(pkg: DocxPackage) -> int:
    count = 0
    for name in pkg.names():
        if re.match(r"^word/(charts|diagrams)/.*\.xml$", name):
            try:
                root = parse_xml(pkg.read(name))
            except Exception:
                continue
            count += sum(1 for el in root.iter(f"{{{A}}}t") if (el.text or "").strip())
    if pkg.has("word/document.xml"):
        root = parse_xml(pkg.read("word/document.xml"))
        count += sum(1 for el in root.iter(f"{{{A}}}t") if (el.text or "").strip())
    return count


# ---------------------------------------------------------------- rebuild


def _apply_lang(rpr: etree._Element, lang: LanguageInfo) -> None:
    el = rpr.find(w("lang"))
    if el is None:
        el = etree.SubElement(rpr, w("lang"))
    if lang.script in CJK_SCRIPTS:
        el.set(w("eastAsia"), lang.code)
    elif lang.rtl:
        el.set(w("bidi"), lang.code)
        if rpr.find(w("rtl")) is None:
            etree.SubElement(rpr, w("rtl"))
    else:
        el.set(w("val"), lang.code)


def _new_run(rpr: etree._Element | None, lang: LanguageInfo | None) -> etree._Element:
    r = etree.Element(w("r"))
    if rpr is not None or lang is not None:
        new_rpr = copy.deepcopy(rpr) if rpr is not None else etree.Element(w("rPr"))
        if lang is not None:
            _apply_lang(new_rpr, lang)
        r.append(new_rpr)
    return r


def _append_text(container: etree._Element, text: str, rpr, lang) -> None:
    text = text.replace("\r", "").replace("\n", " ")
    pieces = text.split("\t")
    r = _new_run(rpr, lang)
    for i, piece in enumerate(pieces):
        if i:
            etree.SubElement(r, w("tab"))
        if piece:
            t = etree.SubElement(r, w("t"))
            t.text = piece
            t.set(f"{{{XML_NS}}}space", "preserve")
    container.append(r)


def rebuild_paragraph(handle: _Handle, tagged: str, lang: LanguageInfo | None) -> None:
    holder = etree.Element("holder")
    containers = [holder]
    fmt: list[tuple[str, etree._Element | None]] = []
    used_x: set[str] = set()
    for tok in T.tokenize(tagged):
        if tok.kind == "text":
            if not tok.value:
                continue
            rpr = fmt[-1][1] if fmt else handle.base_rpr
            _append_text(containers[-1], tok.value, rpr, lang)
        elif tok.kind == "open":
            spec = handle.specs.get(tok.value)
            if spec is None:
                continue
            if spec.kind == "g":
                fmt.append((tok.value, spec.rpr))
            else:
                shell = copy.deepcopy(spec.shell)
                containers[-1].append(shell)
                inner = shell.find(w("sdtContent")) if spec.content_path == "sdtContent" else shell
                containers.append(inner if inner is not None else shell)
        elif tok.kind == "close":
            spec = handle.specs.get(tok.value)
            if spec is None:
                continue
            if spec.kind == "g":
                for i in range(len(fmt) - 1, -1, -1):
                    if fmt[i][0] == tok.value:
                        del fmt[i]
                        break
            elif len(containers) > 1:
                containers.pop()
        elif tok.kind == "empty":
            spec = handle.specs.get(tok.value)
            if spec is None:
                continue
            reuse = tok.value not in used_x
            used_x.add(tok.value)
            for obj, rpr, level in spec.objects:
                el = obj if reuse else copy.deepcopy(obj)
                if level == "para":
                    containers[-1].append(el)
                else:
                    r = _new_run(rpr, None)
                    r.append(el)
                    containers[-1].append(r)
    p = handle.p
    for child in list(p):
        if local(child) != "pPr":
            p.remove(child)
    for child in list(holder):
        p.append(child)
    if lang is not None and lang.rtl:
        ppr = p.find(w("pPr"))
        if ppr is None:
            ppr = etree.Element(w("pPr"))
            p.insert(0, ppr)
        if ppr.find(w("bidi")) is None:
            ppr.append(etree.Element(w("bidi")))


def rebuild(original: bytes, translations: dict[str, str], lang: LanguageInfo | None) -> bytes:
    """Return a new DOCX with each segment id replaced by its translated tagged text.

    Segments without a translation keep their original content untouched.
    """
    ex = extract(original)
    for seg in ex.segments:
        if seg.sid in translations:
            rebuild_paragraph(ex.handles[seg.sid], translations[seg.sid], lang)
    for part, root in ex.roots.items():
        ex.pkg.write(part, serialize_xml(root))
    if lang is not None:
        _set_document_language(ex.pkg, lang)
    _request_field_update(ex.pkg)
    return ex.pkg.to_bytes()


def _set_document_language(pkg: DocxPackage, lang: LanguageInfo) -> None:
    if not pkg.has("word/styles.xml"):
        return
    root = parse_xml(pkg.read("word/styles.xml"))
    defaults = root.find(w("docDefaults"))
    if defaults is None:
        return
    rpr_default = defaults.find(w("rPrDefault"))
    if rpr_default is None:
        rpr_default = etree.SubElement(defaults, w("rPrDefault"))
    rpr = rpr_default.find(w("rPr"))
    if rpr is None:
        rpr = etree.SubElement(rpr_default, w("rPr"))
    _apply_lang(rpr, lang)
    pkg.write("word/styles.xml", serialize_xml(root))


def _request_field_update(pkg: DocxPackage) -> None:
    """Ask Word to refresh the table of contents, page numbers and cross-references on open."""
    if not pkg.has("word/settings.xml"):
        return
    root = parse_xml(pkg.read("word/settings.xml"))
    el = root.find(w("updateFields"))
    if el is None:
        el = etree.Element(w("updateFields"))
        root.insert(0, el)
    el.set(w("val"), "true")
    pkg.write("word/settings.xml", serialize_xml(root))


def formatting_inventory(seg: Segment, tagged: str | None = None) -> dict[str, Counter]:
    """Formatting present in a paragraph: g styles, k wrappers, x objects."""
    ids = T.tag_ids(seg.tagged if tagged is None else tagged)
    inv: dict[str, Counter] = {"g": Counter(), "k": Counter(), "x": Counter()}
    for tid in ids:
        kind = tid[0]
        inv[kind][seg.tag_keys.get(tid, tid)] += 1
    return inv
