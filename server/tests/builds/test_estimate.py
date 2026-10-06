"""What a build costs before it runs, ported from
`opennotebook_server/src/estimate.rs` and `estimate_live.rs`. Plan §9: the
estimates match the measured builds."""

from dataclasses import replace

from opennotebook.ai.prices import Price
from opennotebook.domain import sessions_estimate as e
from opennotebook.domain.sessions_estimate import (
    CHARS_PER_TOKEN,
    GROUP_RESEARCH,
    GROUP_SLIDES,
    QA_OUT_FLOOR,
    RESEARCH_USD,
    Inputs,
    Range,
    cent_up,
    estimate,
    grouped,
    over_limit_message,
    slide_design,
    tokens,
)
from opennotebook.memory.qa import MAX_WINDOWS_PER_DOC
from opennotebook.script.budget import slide_narration


def prices() -> dict[str, Price]:
    """OpenRouter's prices on 2026-10-02 for the models a default build uses."""
    return {
        "amazon/nova-micro-v1": Price(0.035e-6, 0.14e-6),
        "openai/gpt-4o-mini": Price(0.15e-6, 0.6e-6),
        "anthropic/claude-haiku-4.5": Price(1e-6, 5e-6),
        "anthropic/claude-sonnet-5.5": Price(2e-6, 10e-6),
    }


def inputs() -> Inputs:
    """The session the spending limit was set against: four sources, 33k
    characters, five slides, two voices, 20 minutes, on the default model."""
    return Inputs(
        source_chars=[12_000, 9_000, 7_000, 5_320],
        slides=5,
        speakers=2,
        script_model="amazon/nova-micro-v1",
        slide_model="anthropic/claude-haiku-4.5",
        slide_narration=slide_narration(20, 5),
        minutes=20,
    )


def step(est: e.Estimate, name: str) -> e.Line:
    return next(ln for ln in est.lines if ln.step == name)


def test_an_audio_overview_pays_for_no_slides() -> None:
    slides = estimate(inputs(), prices())
    audio = estimate(replace(inputs(), audio=True), prices())
    assert any(ln.group == GROUP_SLIDES for ln in slides.lines)
    assert not any(ln.group == GROUP_SLIDES for ln in audio.lines)
    assert audio.total[1] < slides.total[1]


def test_an_audio_overview_is_described_in_chapters() -> None:
    est = estimate(replace(inputs(), audio=True), prices())
    for ln in est.lines:
        assert "slide" not in ln.step and "slide" not in ln.detail, (ln.step, ln.detail)
    assert step(est, "Outline").detail.startswith("5 chapter titles")
    assert any(ln.step == "Lengthening short chapters" for ln in est.lines)


def test_the_qa_line_says_it_is_in_recorded_spend() -> None:
    est = estimate(inputs(), prices())
    assert (
        "these calls are included in recorded spend"
        in step(est, "Question & answer extraction").detail
    )


def test_the_range_is_ordered_and_every_model_is_priced() -> None:
    est = estimate(inputs(), prices())
    assert est.total[0] <= est.total[1] <= est.total[2], est.total
    assert not any(ln.unpriced for ln in est.lines)


def test_a_twenty_minute_five_slide_session_stays_well_under_fifty_cents() -> None:
    # Plan §9: "a twenty-minute session stays under fifty cents".
    est = estimate(inputs(), prices())
    assert est.total[2] < 0.25, est.total
    # Written one slide at a time at this length.
    assert step(est, "Narration script").calls == Range.exact(5)


def test_slides_go_four_to_a_call_on_the_slide_model() -> None:
    i = replace(inputs(), slides=9)
    design = step(estimate(i, prices()), "Slide design")
    assert design.model == "anthropic/claude-haiku-4.5"
    assert design.calls == Range(3, 3, 6)
    sonnet = step(
        estimate(replace(i, slide_model="anthropic/claude-sonnet-5.5"), prices()), "Slide design"
    )
    assert sonnet.cost[1] > design.cost[1], "Sonnet costs more than Haiku"


def test_qa_extraction_reads_every_file_whole_four_times() -> None:
    qa = estimate(replace(inputs(), source_chars=[44_000, 12_000]), prices()).lines[0]
    # The 44,000-character file is read in two windows, the other in one.
    assert qa.calls == Range.exact((2 + 1) * 4)
    assert qa.input_tokens == (11_000 + 2 * 650) * 4 + (3_000 + 650) * 4
    # Two windows ask for five pairs each, so write what one call would.
    assert qa.output_tokens.typical == 1000 * 4 + 1000 * 4


def test_a_very_long_file_is_read_in_at_most_six_windows() -> None:
    qa = step(
        estimate(replace(inputs(), source_chars=[1_000_000]), prices()),
        "Question & answer extraction",
    )
    assert qa.calls == Range.exact(MAX_WINDOWS_PER_DOC * 4)
    # Six windows ask for four pairs each: 24 where one call asks for 10.
    assert qa.output_tokens.typical == 1000 * 24 // 10 * 4


def test_a_model_with_no_price_is_marked_not_hidden() -> None:
    est = estimate(replace(inputs(), slide_model="someone/unknown-model"), prices())
    assert any(ln.unpriced for ln in est.lines)
    assert any("no price" in a for a in est.assumptions)
    assert grouped(44_072) == "44,072"
    assert grouped(999) == "999"


def test_free_steps_are_listed_not_dropped() -> None:
    # The search index, the style kit and the narration audio.
    assert sum(1 for ln in estimate(inputs(), prices()).lines if ln.free) == 3


def test_slides_are_priced_by_the_batches_they_go_out_in() -> None:
    # Five slides go out as a batch of four and a batch of one: two prompts,
    # five slides' copy, five slides written. Not two full batches of four.
    calls, input_, output = slide_design(5)
    assert calls == Range(2, 2, 4)
    assert input_.typical == tokens(2 * 5_400 + 5 * 700)
    assert output.typical == tokens(5 * 2_200)
    assert input_.high == 2 * input_.typical, "every batch asked twice"
    one, _, out1 = slide_design(1)
    assert one.typical == 1
    assert out1.typical == tokens(2_200)


def test_a_five_slide_deck_matches_the_measured_builds() -> None:
    # Plan §9. The calibration holds against what eight real 5-slide decks
    # did on Haiku 4.5 (prep jobs 00ks to 00m1): 11k to 14k characters sent,
    # 9k to 13k written.
    _, input_, output = slide_design(5)

    def chars(t: int) -> int:
        return t * int(CHARS_PER_TOKEN)

    assert 11_000 <= chars(input_.typical) <= 15_000
    assert 9_000 <= chars(output.typical) <= 14_000
    assert chars(output.low) <= 9_000 and chars(output.high) >= 13_000


def test_a_tiny_source_is_not_priced_as_ten_full_answers() -> None:
    i = replace(inputs(), source_chars=[140, 249, 223])
    qa = step(estimate(i, prices()), "Question & answer extraction")
    assert qa.calls == Range.exact(12)
    assert qa.output_tokens == Range.exact(12 * QA_OUT_FLOOR)
    # A large source still gets the full range.
    big = step(
        estimate(replace(i, source_chars=[40_000]), prices()), "Question & answer extraction"
    )
    assert big.output_tokens == Range(4 * 250, 4 * 1000, 4 * 1800)


def test_lengthening_reads_the_material_a_part_was_given() -> None:
    small = replace(inputs(), source_chars=[600], slide_narration=892)
    big = replace(small, source_chars=[60_000])
    s = step(estimate(small, prices()), "Lengthening short slides").input_tokens
    b = step(estimate(big, prices()), "Lengthening short slides").input_tokens
    assert s < b


def test_research_is_a_line_only_when_the_prep_researches() -> None:
    off = estimate(inputs(), prices())
    assert not any(ln.group == GROUP_RESEARCH for ln in off.lines)
    on = estimate(replace(inputs(), research=True), prices())
    assert step(on, "Web research").cost == RESEARCH_USD
    assert abs(on.total[1] - off.total[1] - RESEARCH_USD[1]) < 1e-9
    assert any("Web research" in a for a in on.assumptions)


def test_the_three_note_collection_is_priced_near_what_such_builds_cost() -> None:
    # Collection s1791059435652 as the studio had it on 2026-10-03: three
    # notes, five slides, two voices, five minutes, Haiku 4.5 for script and
    # slides. The old estimate said $0.080 / $0.129 / $0.335.
    i = Inputs(
        source_chars=[140, 249, 223],
        slides=5,
        speakers=2,
        script_model="anthropic/claude-haiku-4.5",
        slide_model="anthropic/claude-haiku-4.5",
        slide_narration=slide_narration(5, 5),
        minutes=5,
    )
    est = estimate(i, prices())
    assert est.total[1] < 0.129 and est.total[2] < 0.335, est.total
    assert step(est, "Slide design").cost[1] < 0.03


# ── the live half ────────────────────────────────────────────────────────────


def test_the_limit_message_points_at_the_settings_tab() -> None:
    m = over_limit_message(0.81, 0.5, e.FEWER_SLIDES)
    assert "$0.81" in m and "$0.50" in m, m
    assert "Settings › Costs & limits" in m, m
    assert "shorter length" in over_limit_message(1.0, 0.5, e.SHORTER)


def test_an_amount_over_the_limit_never_reads_as_the_limit() -> None:
    m = over_limit_message(0.2525, 0.25, e.FEWER_SLIDES)
    assert "up to $0.26, over your $0.25" in m, m
    assert cent_up(0.81) == 0.81
    assert cent_up(0.8100000001) == 0.81
    assert cent_up(0.811) == 0.82


def test_counts_and_models_are_said_the_way_a_person_reads_them() -> None:
    assert [e.count_short(n) for n in (850, 1_234, 12_400, 1_200_000)] == [
        "850",
        "1.2k",
        "12k",
        "1.2M",
    ]
    assert e.model_name("anthropic/claude-haiku-4.5") == "Claude Haiku 4.5"
    assert e.model_name("google/gemini-2.5-flash-lite") == "Gemini 2.5 Flash Lite"
    assert e.model_name("local") == "Local"
    assert (e.sources_said(1), e.sources_said(3)) == ("1 source", "3 sources")
