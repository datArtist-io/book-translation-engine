import io
import zipfile

import pytest

from bte.docx.package import DocxPackage, DocxRejected
from bte.docx.segments import extract, formatting_inventory, rebuild
from bte.languages import get_language
from bte.text import tags as T

from .fixtures import make_docx


def by_plain(ex, text):
    return next(s for s in ex.segments if s.plain == text)


def test_extracts_formatting_as_tags():
    ex = extract(make_docx())
    seg = next(s for s in ex.segments if s.plain.startswith("Adebayo sold"))
    assert seg.tagged == (
        "Adebayo sold <g1>12 baskets</g1> of <g2>red</g2> peppers before noon, "
        "but he did <g3>not</g3> sell the yams.<x4/>"
    )
    assert seg.tag_labels["g1"] == "bold"
    assert "italic" in seg.tag_labels["g2"] and "C00000" in seg.tag_labels["g2"]
    assert seg.tag_labels["g3"] == "underline"
    assert seg.tag_labels["x4"] == "footnote marker"


def test_hyperlink_tab_break_field_and_escaping():
    ex = extract(make_docx())
    link = next(s for s in ex.segments if s.plain.startswith("Visit"))
    assert link.tagged == "Visit <k1><g2>our website</g2></k1> or call 0803 555 0199."
    price = next(s for s in ex.segments if s.plain.startswith("Price"))
    assert price.tagged == "Price:<x1/>₦2,500 &amp; tax &lt;included&gt;<x2/>Next line."
    assert price.plain == "Price:₦2,500 & tax <included>Next line."
    page = next(s for s in ex.segments if s.plain.startswith("Page"))
    # field begin + code + separate merge into one opaque tag; visible result stays text
    assert page.tagged == "Page <x1/>3<x2/>"


def test_tracked_changes_accepted_and_reported():
    ex = extract(make_docx())
    seg = next(s for s in ex.segments if s.plain.startswith("The market"))
    assert seg.plain == "The market was very busy."
    assert any("tracked deletion" in w for w in ex.warnings)


def test_parts_headings_and_translatability():
    ex = extract(make_docx())
    parts = {s.part for s in ex.segments}
    assert {"word/document.xml", "word/footnotes.xml", "word/header1.xml"} <= parts
    assert by_plain(ex, "The River and the Market").heading_level in (0, None)
    assert by_plain(ex, "Chapter One").heading_level == 1
    image_only = [s for s in ex.segments if s.tagged == "<x1/>"]
    assert image_only and not image_only[0].translatable
    assert not by_plain(ex, "2024").translatable
    # footnote separators are not segments
    assert not any(s.plain == "" and s.part.endswith("footnotes.xml") for s in ex.segments)


def test_extraction_is_deterministic():
    a, b = extract(make_docx()), extract(make_docx())
    assert [(s.sid, s.tagged) for s in a.segments] == [(s.sid, s.tagged) for s in b.segments]


def test_identity_round_trip_is_lossless():
    src = make_docx()
    ex = extract(src)
    out = rebuild(src, {s.sid: s.tagged for s in ex.segments}, None)
    ex2 = extract(out)
    assert [(s.sid, s.tagged) for s in ex.segments] == [(s.sid, s.tagged) for s in ex2.segments]
    for s1, s2 in zip(ex.segments, ex2.segments):
        assert formatting_inventory(s1) == formatting_inventory(s2)


def test_translated_rebuild_moves_formatting_with_words():
    src = make_docx()
    ex = extract(src)
    seg = next(s for s in ex.segments if s.plain.startswith("Adebayo sold"))
    # Formatting tags reordered as a translator would (adjective after noun)
    translated = "Adébáyọ̀ ta <g1>agbọ̀n 12</g1> ata <g2>pupa</g2> kí ọ̀sán tó dé, ṣùgbọ́n kò <g3>tà</g3> iṣu.<x4/>"
    yo = get_language("yo")
    out = rebuild(src, {seg.sid: translated}, yo)
    ex2 = extract(out)
    seg2 = next(s for s in ex2.segments if s.sid == seg.sid)
    assert seg2.tagged == translated
    assert seg2.tag_labels["g1"] == "bold"
    assert seg2.tag_labels["x4"] == "footnote marker"
    doc = DocxPackage.from_bytes(out).read("word/document.xml").decode()
    assert 'w:val="yo"' in doc  # language tagged on translated runs
    settings = DocxPackage.from_bytes(out).read("word/settings.xml").decode()
    assert "updateFields" in settings
    # untouched paragraphs are byte-identical in text
    assert by_plain(ex2, "2024").tagged == "2024"


def test_rtl_target_sets_bidi():
    src = make_docx()
    ex = extract(src)
    seg = by_plain(ex, "Chapter One")
    out = rebuild(src, {seg.sid: "الفصل الأول"}, get_language("ar"))
    doc = DocxPackage.from_bytes(out).read("word/document.xml").decode()
    assert "<w:bidi/>" in doc and "<w:rtl/>" in doc


def test_rejects_unsafe_files():
    with pytest.raises(DocxRejected):
        DocxPackage.from_bytes(b"not a zip")
    with pytest.raises(DocxRejected, match="password-protected"):
        DocxPackage.from_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"0" * 100)
    with pytest.raises(DocxRejected, match="Macro"):
        DocxPackage.from_bytes(make_docx(extra={"word/vbaProject.bin": b"x"}))
    evil = make_docx(body='<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>')
    with pytest.raises(DocxRejected, match="DOCTYPE"):
        extract(evil)


def test_rejects_zip_bomb():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", "x")
        z.writestr("word/document.xml", "x")
        z.writestr("word/bomb.bin", b"\0" * (60 * 1024 * 1024))
    with pytest.raises(DocxRejected, match="compression"):
        DocxPackage.from_bytes(buf.getvalue())


def test_tag_validation():
    expected = T.tag_ids("A <g1>b</g1> c<x2/>")
    assert T.validate("A <g1>b</g1> c<x2/>", expected) == []
    assert any("missing" in p for p in T.validate("A b c<x2/>", expected))
    assert any("cross" in p or "never closed" in p for p in T.validate("<g1>A <x2/>", expected))
    assert any("unexpected" in p for p in T.validate("<g1>b</g1><x2/><x2/>", expected))
