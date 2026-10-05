"""The converter's one promise: the document's sentences come out byte for
byte. The fixtures are a Word document and a PDF made from the same text."""

from __future__ import annotations

from opennotebook.convert import InputKind, to_markdown

from .common import FIXTURES

SENTENCES = (
    "The narration engine attaches a spoken script to each slide.",
    "Barge-in cancels the running text-to-speech stream before answering.",
    "Kokoro exposes named voices such as af_bella and am_adam.",
)


def assert_verbatim(md: str) -> None:
    for sentence in SENTENCES:
        assert sentence in md, f"missing {sentence!r} in:\n{md}"


def test_word_text_is_verbatim() -> None:
    md = to_markdown((FIXTURES / "verbatim.docx").read_bytes(), InputKind.WORD)
    assert_verbatim(md)


def test_pdf_text_is_verbatim() -> None:
    md = to_markdown((FIXTURES / "verbatim.pdf").read_bytes(), InputKind.PDF)
    assert_verbatim(md)


def test_a_scan_is_not_an_error_and_has_no_filler() -> None:
    # A PDF of page images: there is nothing to read, and the converter must
    # not invent page headings that would make it look like there was. Ingest
    # refuses anything under 80 visible characters as a scan.
    md = to_markdown((FIXTURES / "scanned.pdf").read_bytes(), InputKind.PDF)
    visible = sum(1 for c in md if not c.isspace())
    assert visible < 80, repr(md)


def test_extensions_name_kinds() -> None:
    assert InputKind.from_extension("DOCX") is InputKind.WORD
    assert InputKind.from_extension("xlsx") is InputKind.EXCEL
    assert InputKind.from_extension("Pptx") is InputKind.POWERPOINT
    assert InputKind.from_extension("pdf") is InputKind.PDF
    assert InputKind.from_extension(".pdf") is InputKind.PDF
    assert InputKind.from_extension("doc") is None
    assert InputKind.from_extension("") is None
