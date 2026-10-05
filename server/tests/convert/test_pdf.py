"""How PDF lines are joined back into paragraphs."""

from __future__ import annotations

from opennotebook.convert.pdf import join_hyphenated, paragraphs


def test_hyphens_go_only_with_evidence() -> None:
    vocab = {"replication"}
    assert join_hyphenated("DNA replica-", "tion starts", vocab) == "DNA replication starts"
    assert join_hyphenated("a well-", "known fact", vocab) == "a well-known fact"
    assert join_hyphenated("Barge-", "In", vocab) == "Barge-In"


def test_lines_join_into_paragraphs() -> None:
    page = (
        "The first line of a paragraph that wraps\n"
        "onto a second line and ends here.\n\nA new paragraph."
    )
    assert paragraphs(page, set()) == [
        "The first line of a paragraph that wraps onto a second line and ends here.",
        "A new paragraph.",
    ]


def test_list_items_stay_apart() -> None:
    page = "Steps to follow when the text is long enough:\n1. first\n2. second"
    assert len(paragraphs(page, set())) == 3
