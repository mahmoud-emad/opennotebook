"""Word documents built part by part, checking each structure the converter
promises to keep."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from opennotebook.convert import ConvertError, InputKind, to_markdown
from opennotebook.convert.docx import hyperlink_target

from .common import package, rels

W = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:v="urn:schemas-microsoft-com:vml"'
)

STYLES = """<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Titre1"><w:name w:val="heading 1"/></w:style>
  <w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="heading 2"/></w:style>
  <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/></w:style>
  <w:style w:type="paragraph" w:styleId="ListBullet"><w:name w:val="List Bullet"/>
    <w:pPr><w:numPr><w:numId w:val="1"/></w:numPr></w:pPr></w:style>
  <w:style w:type="character" w:styleId="Strong"><w:name w:val="Strong"/>
    <w:rPr><w:b/></w:rPr></w:style>
</w:styles>"""

NUMBERING = """<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:abstractNum w:abstractNumId="10">
    <w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl>
    <w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/></w:lvl>
  </w:abstractNum>
  <w:abstractNum w:abstractNumId="20">
    <w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/></w:lvl>
    <w:lvl w:ilvl="1"><w:start w:val="1"/><w:numFmt w:val="lowerLetter"/></w:lvl>
  </w:abstractNum>
  <w:num w:numId="1"><w:abstractNumId w:val="10"/></w:num>
  <w:num w:numId="2"><w:abstractNumId w:val="20"/></w:num>
</w:numbering>"""

FOOTNOTES = """<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:footnote w:type="separator" w:id="-1"><w:p><w:r><w:separator/></w:r></w:p></w:footnote>
  <w:footnote w:id="1"><w:p><w:r><w:footnoteRef/></w:r><w:r><w:t xml:space="preserve"> Measured on the reference laptop.</w:t></w:r></w:p></w:footnote>
</w:footnotes>"""  # noqa: E501


def p(inner: str) -> str:
    return f"<w:p>{inner}</w:p>"


def styled(style: str, text: str) -> str:
    return f'<w:p><w:pPr><w:pStyle w:val="{style}"/></w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>'


def item(num: int, level: int, text: str) -> str:
    return (
        f'<w:p><w:pPr><w:numPr><w:ilvl w:val="{level}"/><w:numId w:val="{num}"/></w:numPr>'
        f"</w:pPr><w:r><w:t>{text}</w:t></w:r></w:p>"
    )


def run(text: str) -> str:
    return f'<w:r><w:t xml:space="preserve">{text}</w:t></w:r>'


def docx(body: str, extra: Sequence[tuple[str, str]] = ()) -> bytes:
    """A document, with extra parts such as footnotes. Every note in a document
    is written out, referenced or not, so notes are only added where a test is
    about them."""
    document = (
        f'<?xml version="1.0" encoding="UTF-8"?><w:document {W}><w:body>{body}<w:sectPr/>'
        "</w:body></w:document>"
    )
    parts = [
        ("_rels/.rels", rels([("rId1", "officeDocument", "word/document.xml")])),
        ("word/document.xml", document),
        (
            "word/_rels/document.xml.rels",
            rels(
                [
                    ("rId1", "styles", "styles.xml"),
                    ("rId2", "numbering", "numbering.xml"),
                    ("rId3", "footnotes", "footnotes.xml"),
                    ("rId9", "hyperlink", "https://example.com/docs?a=1"),
                ]
            ),
        ),
        ("word/styles.xml", STYLES),
        ("word/numbering.xml", NUMBERING),
        *extra,
    ]
    return package(parts)


def convert(body: str) -> str:
    return to_markdown(docx(body), InputKind.WORD)


def test_headings_follow_styles_by_name_not_id() -> None:
    md = convert(
        styled("Title", "The Plan")
        # A localised id, recognised through the style's name.
        + styled("Titre1", "Background")
        + styled("Heading2", "Detail")
        + p(run("Body text."))
    )
    assert md == "# The Plan\n\n# Background\n\n## Detail\n\nBody text.\n"


def test_emphasis_links_breaks_and_tabs() -> None:
    md = convert(
        p(
            run("Plain ")
            + '<w:r><w:rPr><w:b/></w:rPr><w:t xml:space="preserve">bold </w:t></w:r>'
            + "<w:r><w:rPr><w:i/></w:rPr><w:t>italic</w:t></w:r>"
            + '<w:r><w:rPr><w:rStyle w:val="Strong"/></w:rPr>'
            + '<w:t xml:space="preserve"> strong</w:t></w:r>'
            + run(", see ")
            + '<w:hyperlink r:id="rId9"><w:r><w:t>the docs</w:t></w:r></w:hyperlink>'
            + "<w:r><w:br/><w:t>next</w:t><w:tab/><w:t>line</w:t></w:r>"
        )
    )
    assert md == (
        "Plain **bold** *italic* **strong**, see [the docs](https://example.com/docs?a=1)  \n"
        "next line\n"
    )


def test_lists_nest_and_tell_bullets_from_numbers() -> None:
    md = convert(
        p(run("Intro."))
        + item(1, 0, "apples")
        + item(1, 1, "green")
        + item(1, 0, "pears")
        + item(2, 0, "first")
        + item(2, 1, "detail")
        + item(2, 0, "second")
    )
    assert md == "Intro.\n\n- apples\n    - green\n- pears\n1. first\n    1. detail\n2. second\n"


def test_a_list_style_makes_list_items() -> None:
    assert convert(styled("ListBullet", "styled item")) == "- styled item\n"


def test_tables_keep_merges_empty_cells_and_pipes() -> None:
    def cell(props: str, text: str) -> str:
        return f"<w:tc><w:tcPr>{props}</w:tcPr><w:p>{run(text) if text else ''}</w:p></w:tc>"

    table = (
        "<w:tbl>"
        f"<w:tr>{cell('', 'Name')}{cell('', 'Choice')}{cell('', 'Notes')}</w:tr>"
        f"<w:tr>{cell('<w:vMerge w:val="restart"/>', 'Ada')}"
        f"{cell('<w:gridSpan w:val="2"/>', 'a | b')}</w:tr>"
        f"<w:tr>{cell('<w:vMerge/>', '')}{cell('', '')}{cell('', 'last')}</w:tr>"
        "</w:tbl>"
    )
    assert convert(table) == (
        "| Name | Choice | Notes |\n| --- | --- | --- |\n| Ada | a \\| b |  |\n|  |  | last |\n"
    )


def test_footnotes_are_appended_and_referenced() -> None:
    body = p(run("It runs at 40 ms.") + '<w:r><w:footnoteReference w:id="1"/></w:r>')
    md = to_markdown(docx(body, [("word/footnotes.xml", FOOTNOTES)]), InputKind.WORD)
    assert md == "It runs at 40 ms.[^1]\n\n[^1]: Measured on the reference laptop.\n"


def test_text_boxes_are_read_once() -> None:
    # Word writes a text box twice: a DrawingML shape and a VML fallback.
    def box_text(t: str) -> str:
        return f"<w:txbxContent>{p(run(t))}</w:txbxContent>"

    body = p(
        run("Anchor paragraph.")
        + '<w:r><mc:AlternateContent><mc:Choice Requires="wps"><w:drawing><wps:wsp><wps:txbx>'
        + box_text("Inside the box.")
        + "</wps:txbx></wps:wsp></w:drawing></mc:Choice><mc:Fallback><w:pict><v:shape><v:textbox>"
        + box_text("Inside the box.")
        + "</v:textbox></v:shape></w:pict></mc:Fallback></mc:AlternateContent></w:r>"
    )
    assert convert(body) == "Anchor paragraph.\n\nInside the box.\n"


def test_tracked_changes_read_as_accepted() -> None:
    md = convert(
        p(
            run("The ")
            + "<w:del><w:r><w:delText>old </w:delText></w:r></w:del>"
            + '<w:ins><w:r><w:t xml:space="preserve">new </w:t></w:r></w:ins>'
            + run("wording.")
        )
    )
    assert md == "The new wording.\n"


def test_fields_keep_their_result() -> None:
    md = convert(
        p(
            run("Dated ")
            + '<w:fldSimple w:instr=" DATE "><w:r><w:t>4 October</w:t></w:r></w:fldSimple>'
            + '<w:r><w:fldChar w:fldCharType="begin"/></w:r>'
            + '<w:r><w:instrText xml:space="preserve"> HYPERLINK "https://example.org" '
            + "</w:instrText></w:r>"
            + '<w:r><w:fldChar w:fldCharType="separate"/></w:r>'
            + run(" example")
            + '<w:r><w:fldChar w:fldCharType="end"/></w:r>'
        )
    )
    assert md == "Dated 4 October [example](https://example.org)\n"


def test_markdown_characters_are_not_escaped() -> None:
    md = convert(p(run("voices af_bella and am_adam cost 5 * 3 &amp; more")))
    assert md == "voices af_bella and am_adam cost 5 * 3 & more\n"


def test_a_zip_without_a_document_is_not_a_word_file() -> None:
    with pytest.raises(ConvertError) as caught:
        to_markdown(package([("hello.txt", "hi")]), InputKind.WORD)
    assert str(caught.value).startswith("This file is not a valid Word document.")


def test_hyperlink_fields_yield_their_url() -> None:
    assert hyperlink_target(' HYPERLINK "https://example.com/x" \\o "tip" ') == (
        "https://example.com/x"
    )
    assert hyperlink_target('HYPERLINK \\l "_Toc1"') is None
    assert hyperlink_target("PAGE") is None
