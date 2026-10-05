"""Ported from opennotebook_script/src/grounding.rs (excerpt_tests) and the
`fit` tests of budget.rs."""

from opennotebook.script.budget import TITLE, fit
from opennotebook.script.grounding import excerpts, excerpts_from, passages, terms


def test_a_menu_is_not_material() -> None:
    doc = (
        "Courses\n\nTutorials\n\nInterview Prep\n\nThe scheduler picks the next "
        "process to run from the run queue each time a slice ends."
    )
    got = excerpts([doc], "process scheduler", 6)
    assert len(got) == 1
    assert got[0].startswith("The scheduler")


def test_only_the_pieces_about_the_topic_are_kept() -> None:
    def para(s: str) -> str:
        return f"{s} {'filler words that pad the paragraph out ' * 30}"

    doc = "\n\n".join(
        [
            para("Zombie processes have exited but not been reaped."),
            para("The task_struct holds everything the kernel knows about a process."),
            para("Printers were once shared through spooling daemons."),
        ]
    )
    got = excerpts([doc], "The task_struct data", 1)
    assert len(got) == 1
    assert "task_struct holds" in got[0]


def test_a_query_that_matches_nothing_still_grounds_on_the_best_document() -> None:
    docs = [
        "One two three four five six seven eight nine.",
        "Ten eleven twelve thirteen fourteen fifteen sixteen seventeen.",
    ]
    assert excerpts(docs, "quantum", 6) == [docs[0]]


def test_each_excerpt_knows_its_document_and_comes_in_document_order() -> None:
    docs = [
        "Kelp forests grow along cold coasts and shelter otters in their canopy.",
        "Coral reefs shelter a quarter of marine species on a tiny share of the floor.",
    ]
    got = excerpts_from(docs, "coral reefs shelter", 8)
    assert [d for d, _ in got] == [0, 1]
    assert passages(docs) == [(0, docs[0]), (1, docs[1])]


def test_terms_are_the_words_worth_matching() -> None:
    assert terms("What is the task_struct, and why does it matter? Matter!") == [
        "matter",
        "task_struct",
    ]


def test_a_string_within_budget_is_untouched() -> None:
    assert fit("  Each slide has spoken words  ", TITLE) == "Each slide has spoken words"


def test_an_overlong_string_is_cut_at_a_word_boundary() -> None:
    long = (
        "The narration engine attaches a spoken script to each slide and every line "
        "carries a speaker"
    )
    cut = fit(long, TITLE)
    assert len(cut) <= TITLE, f"budget must hold, got {len(cut)}"
    assert not cut[-1].isspace()
    assert long.startswith(cut), "the cut must be a prefix of the original"


def test_a_single_overlong_word_is_still_cut() -> None:
    assert len(fit("a" * (TITLE + 40), TITLE)) == TITLE
