"""The shared Markdown writer."""

from __future__ import annotations

from opennotebook.convert.markdown import Item, Span, inline, render, table


def test_emphasis_keeps_spaces_outside_the_markers() -> None:
    spans = [Span("plain "), Span("bold ", bold=True), Span("both", bold=True, italic=True)]
    assert inline(spans) == "plain **bold** ***both***"


def test_a_link_wraps_its_runs() -> None:
    url = "https://example.com/a b"
    spans = [Span("the ", link=url), Span("site", bold=True, link=url)]
    assert inline(spans) == "[the **site**](https://example.com/a%20b)"


def test_deep_items_never_skip_levels() -> None:
    assert render([Item(0, None, "a"), Item(3, None, "b")]) == "- a\n    - b"


def test_tables_pad_rows() -> None:
    assert table([["a", "b"], ["c"]]) == "| a | b |\n| --- | --- |\n| c |  |"
