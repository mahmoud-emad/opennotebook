"""PowerPoint decks built part by part."""

from __future__ import annotations

import pytest

from opennotebook.convert import ConvertError, InputKind, to_markdown

from .common import package, rels

NS = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)


def shape(ph: str, paragraphs: str) -> str:
    """A shape; `ph` is the placeholder element, or empty for a free text box."""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="s"/><p:cNvSpPr/><p:nvPr>{ph}</p:nvPr></p:nvSpPr>'
        f"<p:spPr/><p:txBody><a:bodyPr/>{paragraphs}</p:txBody></p:sp>"
    )


def para(level: int, text: str) -> str:
    return f'<a:p><a:pPr lvl="{level}"/><a:r><a:rPr lang="en-US"/><a:t>{text}</a:t></a:r></a:p>'


def slide(shapes: str) -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><p:sld {NS}><p:cSld><p:spTree><p:nvGrpSpPr/>'
        f"<p:grpSpPr/>{shapes}</p:spTree></p:cSld></p:sld>"
    )


def title(text: str) -> str:
    return shape('<p:ph type="title"/>', para(0, text))


def table() -> str:
    def cell(t: str) -> str:
        return f"<a:tc><a:txBody><a:bodyPr/><a:p><a:r><a:t>{t}</a:t></a:r></a:p></a:txBody></a:tc>"

    return (
        "<p:graphicFrame><p:nvGraphicFramePr/><a:graphic>"
        '<a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table"><a:tbl>'
        f"<a:tr>{cell('Voice')}{cell('Speed')}</a:tr>"
        f'<a:tr>{cell("af_bella | slow")}<a:tc hMerge="1"><a:txBody><a:bodyPr/><a:p/></a:txBody>'
        "</a:tc></a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame>"
    )


def deck() -> bytes:
    """A deck whose slide parts are named against their order: `slide1.xml` is
    shown second."""
    presentation = (
        f'<?xml version="1.0" encoding="UTF-8"?><p:presentation {NS}><p:sldIdLst>'
        '<p:sldId id="256" r:id="rId3"/><p:sldId id="257" r:id="rId2"/>'
        "</p:sldIdLst></p:presentation>"
    )
    first = slide(
        title("Opening")
        + shape(
            '<p:ph idx="1"/>',
            para(0, "Narration") + para(1, "spoken script per slide") + para(0, "Barge-in"),
        )
        + shape(
            "",
            '<a:p><a:r><a:rPr b="1"/><a:t>Read</a:t></a:r>'
            '<a:r><a:t xml:space="preserve"> the </a:t></a:r>'
            '<a:r><a:rPr><a:hlinkClick r:id="rId7"/></a:rPr><a:t>guide</a:t></a:r></a:p>',
        )
    )
    second = slide(
        title("Voices") + table() + shape('<p:ph type="sldNum" idx="12"/>', para(0, "2"))
    )
    notes = (
        f'<?xml version="1.0" encoding="UTF-8"?><p:notes {NS}><p:cSld><p:spTree>'
        + shape('<p:ph type="sldImg"/>', "")
        + shape(
            '<p:ph type="body" idx="1"/>',
            para(0, "Pause after the title.") + para(0, "Then ask a question."),
        )
        + "</p:spTree></p:cSld></p:notes>"
    )
    return package(
        [
            ("_rels/.rels", rels([("rId1", "officeDocument", "ppt/presentation.xml")])),
            ("ppt/presentation.xml", presentation),
            (
                "ppt/_rels/presentation.xml.rels",
                rels(
                    [("rId2", "slide", "slides/slide1.xml"), ("rId3", "slide", "slides/slide2.xml")]
                ),
            ),
            ("ppt/slides/slide1.xml", second),
            ("ppt/slides/slide2.xml", first),
            (
                "ppt/slides/_rels/slide2.xml.rels",
                rels(
                    [
                        ("rId5", "notesSlide", "../notesSlides/notesSlide1.xml"),
                        ("rId7", "hyperlink", "https://example.com/guide"),
                    ]
                ),
            ),
            ("ppt/notesSlides/notesSlide1.xml", notes),
        ]
    )


def test_slides_follow_the_presentation_order() -> None:
    md = to_markdown(deck(), InputKind.POWERPOINT)
    opening = md.find("## Slide 1: Opening")
    voices = md.find("## Slide 2: Voices")
    assert 0 <= opening < voices, md


def test_a_slide_keeps_bullets_tables_links_and_notes() -> None:
    md = to_markdown(deck(), InputKind.POWERPOINT)
    assert md == (
        "## Slide 1: Opening\n\n"
        "- Narration\n    - spoken script per slide\n- Barge-in\n\n"
        "**Read** the [guide](https://example.com/guide)\n\n"
        "Notes:\n\nPause after the title.\n\nThen ask a question.\n\n"
        "## Slide 2: Voices\n\n"
        "| Voice | Speed |\n| --- | --- |\n| af_bella \\| slow |  |\n"
    )


def test_a_deck_without_a_presentation_part_is_refused() -> None:
    data = package([("ppt/slides/slide1.xml", slide(title("Lost")))])
    with pytest.raises(ConvertError) as caught:
        to_markdown(data, InputKind.POWERPOINT)
    assert str(caught.value).startswith("This file is not a valid PowerPoint presentation.")
