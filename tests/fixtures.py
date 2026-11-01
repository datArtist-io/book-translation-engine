"""Build realistic DOCX test documents from raw XML, so every formatting trap is controlled."""

from __future__ import annotations

import io
import zipfile

W_NS = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" ' \
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
<Override PartName="/word/footnotes.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"/>
<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/settings" Target="settings.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes" Target="footnotes.xml"/>
<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/header" Target="header1.xml"/>
<Relationship Id="rId9" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.org/" TargetMode="External"/>
</Relationships>"""

STYLES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles {W_NS}>
<w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Georgia" w:hAnsi="Georgia"/><w:lang w:val="en-GB"/></w:rPr></w:rPrDefault></w:docDefaults>
<w:style w:type="paragraph" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>
</w:styles>"""

SETTINGS = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:settings {W_NS}><w:zoom w:percent="100"/></w:settings>"""

FOOTNOTES = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:footnotes {W_NS}>
<w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>
<w:footnote w:type="continuationSeparator" w:id="0"><w:p><w:r><w:continuationSeparator/></w:r></w:p></w:footnote>
<w:footnote w:id="1"><w:p><w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:footnoteRef/></w:r><w:r><w:t xml:space="preserve"> See the </w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>Annual Report</w:t></w:r><w:r><w:t>, page 12.</w:t></w:r></w:p></w:footnote>
</w:footnotes>"""

HEADER = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:hdr {W_NS}><w:p><w:r><w:t>The River and the Market</w:t></w:r></w:p></w:hdr>"""


def body_xml() -> str:
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document {W_NS}><w:body>
<w:p><w:pPr><w:pStyle w:val="Title"/></w:pPr><w:r><w:t>The River and the Market</w:t></w:r></w:p>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Chapter One</w:t></w:r></w:p>
<w:p><w:pPr><w:jc w:val="both"/><w:spacing w:after="120"/></w:pPr>
  <w:r><w:t xml:space="preserve">Adebayo sold </w:t></w:r>
  <w:r><w:rPr><w:b/></w:rPr><w:t>12 baskets</w:t></w:r>
  <w:r><w:t xml:space="preserve"> of </w:t></w:r>
  <w:r><w:rPr><w:i/><w:color w:val="C00000"/></w:rPr><w:t>red</w:t></w:r>
  <w:r><w:t xml:space="preserve"> peppers before noon, but he did </w:t></w:r>
  <w:r><w:rPr><w:u w:val="single"/></w:rPr><w:t>not</w:t></w:r>
  <w:r><w:t xml:space="preserve"> sell the yams.</w:t></w:r>
  <w:r><w:rPr><w:vertAlign w:val="superscript"/></w:rPr><w:footnoteReference w:id="1"/></w:r>
</w:p>
<w:p><w:r><w:t xml:space="preserve">Visit </w:t></w:r><w:hyperlink r:id="rId9"><w:r><w:rPr><w:rStyle w:val="Hyperlink"/></w:rPr><w:t>our website</w:t></w:r></w:hyperlink><w:r><w:t xml:space="preserve"> or call 0803 555 0199.</w:t></w:r></w:p>
<w:p><w:r><w:t>Price:</w:t></w:r><w:r><w:tab/></w:r><w:r><w:t xml:space="preserve">₦2,500 &amp; tax &lt;included&gt;</w:t></w:r><w:r><w:br/></w:r><w:r><w:t>Next line.</w:t></w:r></w:p>
<w:p><w:r><w:t xml:space="preserve">Page </w:t></w:r><w:r><w:fldChar w:fldCharType="begin"/></w:r><w:r><w:instrText xml:space="preserve"> PAGE </w:instrText></w:r><w:r><w:fldChar w:fldCharType="separate"/></w:r><w:r><w:t>3</w:t></w:r><w:r><w:fldChar w:fldCharType="end"/></w:r></w:p>
<w:p><w:r><w:t xml:space="preserve">The market was </w:t></w:r><w:ins w:id="5" w:author="Ed"><w:r><w:t>very </w:t></w:r></w:ins><w:del w:id="6" w:author="Ed"><w:r><w:delText>quite </w:delText></w:r></w:del><w:r><w:t>busy.</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Item</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:rPr><w:b/></w:rPr><w:t>Quantity</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
<w:p><w:r><w:drawing><wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"/></w:drawing></w:r></w:p>
<w:p><w:r><w:t>2024</w:t></w:r></w:p>
<w:sectPr><w:headerReference w:type="default" r:id="rId4"/><w:pgSz w:w="11906" w:h="16838"/></w:sectPr>
</w:body></w:document>"""


def make_docx(body: str | None = None, extra: dict[str, bytes] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/_rels/document.xml.rels", DOC_RELS)
        z.writestr("word/document.xml", body or body_xml())
        z.writestr("word/styles.xml", STYLES)
        z.writestr("word/settings.xml", SETTINGS)
        z.writestr("word/footnotes.xml", FOOTNOTES)
        z.writestr("word/header1.xml", HEADER)
        for name, data in (extra or {}).items():
            z.writestr(name, data)
    return buf.getvalue()


def long_body(paragraphs: int = 40) -> str:
    sentences = [
        "Adebayo walked to the market at dawn.",
        "He carried 3 baskets of tomatoes and 2 bags of rice.",
        "The trader did not agree to the price.",
        "If it rains, the road will close.",
        "Everyone must arrive before 7 o'clock.",
    ]
    paras = []
    for i in range(paragraphs):
        if i % 10 == 0:
            paras.append(f'<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Chapter {i // 10 + 1}</w:t></w:r></w:p>')
        s = " ".join(sentences[(i + j) % 5] for j in range(3))
        paras.append(
            f'<w:p><w:r><w:t xml:space="preserve">{s} </w:t></w:r>'
            f'<w:r><w:rPr><w:b/></w:rPr><w:t>Section {i + 1}</w:t></w:r><w:r><w:t>.</w:t></w:r></w:p>'
        )
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document {W_NS}><w:body>{''.join(paras)}<w:sectPr/></w:body></w:document>"""
