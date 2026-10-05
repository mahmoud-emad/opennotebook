"""The zip container shared by the Office formats."""

from __future__ import annotations

from opennotebook.convert.ooxml import resolve


def test_targets_resolve_against_the_part_directory() -> None:
    assert resolve("ppt/", "slides/slide1.xml") == "ppt/slides/slide1.xml"
    assert (
        resolve("ppt/slides/", "../notesSlides/notesSlide1.xml")
        == "ppt/notesSlides/notesSlide1.xml"
    )
    assert resolve("", "word/document.xml") == "word/document.xml"
    assert resolve("word/", "/word/footnotes.xml") == "word/footnotes.xml"
