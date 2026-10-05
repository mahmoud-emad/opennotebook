# ruff: noqa: E501
"""Web search and deep research, ported from `research.rs` and the search in
`agent.rs`."""

from opennotebook import research


def test_sonar_citations_become_hits_with_the_answer_as_context() -> None:
    raw = {
        "citations": [
            "https://en.wikipedia.org/wiki/Computer",
            "https://en.wikipedia.org/wiki/Computer",
            "https://www.britannica.com/technology/computer",
        ]
    }
    hits = research.citations(raw, "Computers process data.")
    assert len(hits) == 2, "duplicates dropped"
    assert hits[0].url == "https://en.wikipedia.org/wiki/Computer"
    assert hits[0].snippet == "Computers process data."
    ann = {
        "choices": [
            {
                "message": {
                    "annotations": [{"url_citation": {"url": "https://x.dev/a", "title": "A"}}]
                }
            }
        ]
    }
    assert research.citations(ann, "")[0].title == "A"


def test_the_staged_report_says_what_it_is_and_where_it_came_from() -> None:
    doc = research.document(
        "the mochi paper",
        "Mochi is a speech model.",
        [("Moshi", "https://arxiv.org/abs/2410.00037")],
    )
    assert doc.startswith("# Web research: the mochi paper")
    assert "Mochi is a speech model." in doc
    assert "1. [Moshi](https://arxiv.org/abs/2410.00037)" in doc


def test_planned_queries_are_cleaned_deduplicated_and_capped() -> None:
    got = research.parse_queries(
        '1. Moshi paper arxiv\n- "Moshi speech model"\n\nmoshi paper ARXIV\n3) full duplex\nx\nmore',
        3,
    )
    assert got == ["Moshi paper arxiv", "Moshi speech model", "full duplex"]


def test_depth_sets_the_breadth() -> None:
    assert research.breadth("quick") == (3, 5)
    assert research.breadth("standard") == (5, 12)
    assert research.short_host("https://www.arxiv.org/abs/1") == "arxiv.org"
