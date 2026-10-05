"""The narration budgets, ported from `opennotebook_script/src/budget.rs`.
Plan §9: title 60, line 320, slide narration 650, 17 characters a second,
lines cut at whole sentences."""

from opennotebook.script import budget
from opennotebook.script.budget import (
    BOOKEND_NARRATION,
    CHARS_PER_SECOND,
    LINE,
    MIN_LINE,
    SLIDE_NARRATION,
    TITLE,
    WHOLE_SESSION_MAX_SLIDE,
    exceeds,
    fit,
    fit_spoken,
    room,
    slide_narration,
    speakable,
    whole_sentences,
)


def test_the_budgets_are_the_measured_ones() -> None:
    assert (TITLE, LINE, SLIDE_NARRATION, CHARS_PER_SECOND) == (60, 320, 650, 17)
    assert budget.ELEMENT == 72 and budget.SLIDE_ELEMENTS == 4


def test_twenty_minutes_over_five_slides_is_about_four_thousand_characters_a_slide() -> None:
    per = slide_narration(20, 5)
    assert per == (20 * 60 * CHARS_PER_SECOND - 2 * BOOKEND_NARRATION) // 5
    # At the measured 17 characters a second the whole session is 20 minutes.
    spoken = per * 5 + 2 * BOOKEND_NARRATION
    assert spoken // CHARS_PER_SECOND - 20 * 60 >= -1, spoken
    assert per > WHOLE_SESSION_MAX_SLIDE, "a 20 minute session is written per slide"


def test_a_short_session_keeps_the_depth_a_slide_always_had() -> None:
    assert slide_narration(2, 12) == SLIDE_NARRATION
    assert slide_narration(3, 5) == SLIDE_NARRATION
    assert slide_narration(5, 0) == slide_narration(5, 1)


def test_a_line_that_is_too_long_loses_whole_sentences_not_words() -> None:
    # The line that was spoken as "...making this session an".
    line = (
        "Welcome, everyone. Today, we'll explore the Transformer architecture and how we "
        "measure it. Understanding these elements will empower you to optimize your own "
        "projects, making this session an essential guide."
    )
    assert fit_spoken(line, 200) == (
        "Welcome, everyone. Today, we'll explore the Transformer architecture and how we "
        "measure it."
    )
    # No sentence end inside the budget: a clause, closed as a sentence.
    got = fit_spoken("One very long opening clause about attention, and then a lot more words", 60)
    assert len(got) <= 60
    assert got == "One very long opening clause about attention."
    # Short enough: untouched.
    assert fit_spoken("Short.", 50) == "Short."


def test_a_line_with_no_whole_sentence_in_its_room_is_dropped_not_mangled() -> None:
    # The line a real session ended a slide on, as "...contribute to the."
    q = (
        "How does the 'Inner Monologue' method contribute to the model's ability to handle "
        "overlapping speech in real time?"
    )
    assert whole_sentences(q, 60) is None
    assert (
        whole_sentences("It predicts text first. Then audio follows it.", 30)
        == "It predicts text first."
    )
    assert whole_sentences("Short.", 50) == "Short."


def test_a_budget_spent_to_its_last_few_characters_buys_nothing() -> None:
    assert room(0, SLIDE_NARRATION) == LINE
    assert room(SLIDE_NARRATION - 200, SLIDE_NARRATION) == 200
    # The line that would have been "Moshi util".
    assert room(SLIDE_NARRATION - 10, SLIDE_NARRATION) is None
    assert room(SLIDE_NARRATION, SLIDE_NARRATION) is None
    assert room(SLIDE_NARRATION + 50, SLIDE_NARRATION) is None
    # Exactly the floor is still worth saying.
    assert room(SLIDE_NARRATION - MIN_LINE, SLIDE_NARRATION) == MIN_LINE


def test_a_string_within_budget_is_untouched() -> None:
    assert fit("  Each slide has spoken words  ", TITLE) == "Each slide has spoken words"
    assert not exceeds("short", TITLE)


def test_an_overlong_string_is_cut_at_a_word_boundary() -> None:
    long = (
        "The narration engine attaches a spoken script to each slide and every line carries a "
        "speaker"
    )
    cut = fit(long, TITLE)
    assert len(cut) <= TITLE
    assert not cut[-1].isspace()
    assert long.startswith(cut), "the cut must be a prefix of the original"
    assert exceeds(long, TITLE)


def test_a_single_overlong_word_is_still_cut() -> None:
    # A single word longer than the budget has no boundary to cut at. It must
    # still respect the budget rather than overflow the box.
    assert len(fit("a" * (TITLE + 40), TITLE)) == TITLE


# ── speakable ────────────────────────────────────────────────────────────────


def test_a_dash_becomes_a_stop_the_voice_can_hear() -> None:
    assert speakable("what Moshi is—let's break it down") == "what Moshi is. Let's break it down"
    assert speakable("what Moshi is — let's break it down") == "what Moshi is. Let's break it down"


def test_a_range_is_left_alone() -> None:
    # A range is not a pause. "2014. 2015" would be read as two numbers.
    assert speakable("between 2014–2015 it changed") == "between 2014–2015 it changed"
    assert speakable("pages 3–7") == "pages 3–7"


def test_an_existing_stop_is_not_doubled() -> None:
    assert speakable("It works. — now the why") == "It works. Now the why"


def test_a_leading_dash_is_dropped() -> None:
    assert speakable("— and that is the point") == "and that is the point"


def test_ordinary_text_is_unchanged() -> None:
    t = "Attention lets a token look at every other token. The weights are learned."
    assert speakable(t) == t
