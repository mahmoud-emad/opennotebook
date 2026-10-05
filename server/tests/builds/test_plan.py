"""What a build makes, what it is called, and how a dead build's row is
settled. Ported from `opennotebook_server/src/session_impl.rs`,
`opennotebook_sdk/src/voices.rs` and the playback events of `events.rs`."""

import pytest

from opennotebook.domain import sessions as d
from opennotebook.domain import styles
from opennotebook.domain.sessions import (
    Ask,
    BuildDefaults,
    PlanError,
    Shape,
    Speaker,
    audio_spec,
    output_title,
    plan,
    settle,
    shape,
)
from opennotebook.domain.sessions_events import Head, Lookup, playback_events


def spec(fmt: str, length: str = "") -> d.AudioSpec | None:
    return audio_spec(fmt, length, None)


def test_an_audio_overview_is_shaped_by_its_format_not_the_slide_count() -> None:
    # The spending limit priced an audio overview on the slide count it was
    # sent, while the pipeline built its format's chapters. One rule now.
    deep = shape(12, 2, spec("deep_dive", "longer"))
    assert (deep.slides, deep.speakers) == (6, 2)
    brief = shape(12, 2, spec("brief"))
    assert (brief.slides, brief.speakers) == (2, 1), "Brief is one host"


def test_a_slide_session_takes_its_count_in_range_or_the_default() -> None:
    assert shape(9, 2, None).slides == 9
    assert shape(0, 2, None).slides == 3
    assert shape(30, 2, None).slides == 12
    assert shape(None, 0, None) == Shape(5, 1, None)


def defaults() -> BuildDefaults:
    return BuildDefaults(
        speaker_count="auto",
        host=Speaker("host"),
        second=Speaker("expert"),
        slide_count=6,
        style="editorial",
        audio_format="deep_dive",
        audio_length="longer",
    )


def test_a_plan_clamps_the_slide_count_and_fills_the_gaps() -> None:
    # One range, whoever asks: the dialog's 3 to 12, clamped rather than
    # refused, and the setting when the request says nothing.
    dd = defaults()

    def n(count: int | None) -> int:
        return plan(Ask(slide_count=count, sources=1), dd).slide_count

    assert (n(1), n(30), n(8), n(None)) == (3, 12, 8, 6)
    p = plan(Ask(), dd)
    assert p.style == "editorial"
    assert len(p.speakers) == 1, "auto with one source is one voice"
    assert len(plan(Ask(sources=2), dd).speakers) == 2
    with pytest.raises(PlanError) as e:
        plan(Ask(style="nope"), dd)
    assert str(e.value).endswith(".")
    with pytest.raises(PlanError):
        plan(Ask(speakers=3), dd)


def test_a_plan_gives_an_audio_overview_the_length_setting() -> None:
    # An audio overview takes the length setting when it names none, and its
    # format's voices whatever the speaker setting says.
    p = plan(Ask(audio_format="deep_dive", speakers=1), defaults())
    assert p.audio is not None
    assert p.audio.real_length() == "longer"
    assert p.slide_count == p.audio.chapters()
    assert len(p.speakers) == 2
    p = plan(Ask(audio_format="deep_dive", audio_length="shorter"), defaults())
    assert p.audio is not None and p.audio.real_length() == "shorter"


def test_the_estimate_and_the_limit_price_the_same_build() -> None:
    # The build hands its plan to the pipeline as a slide count and speakers;
    # the estimate prices the plan. Both must come out the same.
    audio = spec("debate", "default")
    assert audio is not None
    p = d.Planned([Speaker("a"), Speaker("b")], audio.chapters(), "", audio)
    assert p.shape() == shape(p.slide_count, len(p.speakers), audio)
    assert p.shape().slides == 4


def test_a_build_with_no_title_is_named_by_what_tells_it_apart() -> None:
    def audio(fmt: str, focus: str) -> d.AudioSpec | None:
        return audio_spec(fmt, None, focus)

    assert output_title(" Mine ", "editorial", None) == "Mine"
    assert output_title("Two\nlines", None, None) == "Twolines"
    editorial = styles.style("editorial")
    assert editorial is not None
    assert output_title("  ", "editorial", None) == f"{editorial.label} slides"
    assert output_title("", None, None) == f"{styles.DEFAULT_STYLE.label} slides"
    assert output_title("", None, audio("brief", "")) == "Brief audio overview"
    assert (
        output_title("", None, audio("debate", " the latency claims "))
        == "Debate audio overview · the latency claims"
    )
    long = output_title("", None, audio("deep_dive", "word " * 40))
    assert long.startswith("Deep Dive audio overview · word"), long
    assert len(long) <= 72, long


def test_a_dead_job_fails_its_row_only_while_the_row_is_there() -> None:
    # Plan §9: reconcile at read time; a deleted output is never resurrected.
    assert settle("preparing", "failed", True) == ("failed", True)
    # Deleted while its job died: writing it back would undelete it.
    assert settle(None, "cancelled", True) == ("preparing", False)
    # Its collection was deleted: the same.
    assert settle("preparing", "failed", False) == ("preparing", False)
    # The prep wrote its own outcome in between: that stands.
    assert settle("ready", "failed", True) == ("ready", False)
    # A job still running is not dead.
    assert settle("preparing", "running", True) == ("preparing", False)


def test_a_role_word_becomes_the_voices_name_and_a_real_name_stays() -> None:
    assert d.display_name("Host", "af_bella") == "Bella"
    assert d.display_name("expert", "am_adam") == "Adam"
    assert d.display_name("", "bm_george") == "George"
    assert d.display_name("Dr. Rana", "af_bella") == "Dr. Rana"
    # No name in the voice id: keep what there is.
    assert d.display_name("Host", "custom") == "Host"


def test_an_audio_overview_s_minutes_and_chapters() -> None:
    def a(fmt: str, length: str) -> d.AudioSpec:
        s = spec(fmt, length)
        assert s is not None
        return s

    assert [a("deep_dive", n).minutes() for n in ("shorter", "default", "longer")] == [5, 10, 16]
    assert [a("deep_dive", n).chapters() for n in ("shorter", "default", "longer")] == [3, 5, 6]
    assert (a("critique", "shorter").minutes(), a("critique", "default").minutes()) == (5, 8)
    assert (a("debate", "shorter").chapters(), a("debate", "default").chapters()) == (3, 4)
    assert (a("brief", "longer").minutes(), a("brief", "longer").chapters()) == (2, 2)
    assert d.parse_format("nonsense") == "deep_dive"
    assert d.format_speakers("brief") == 1


# ── playback events ──────────────────────────────────────────────────────────


def lookup() -> Lookup:
    return Lookup(
        {o: ("c", "p", f"slide{o}") for o in range(3)},
        {
            lid: ("s1", 1234, f"/audio?line={lid}")
            for lid in ("s0l0", "s0l1", "s0l2", "s1l0", "a", "b")
        },
    )


def names(events: list[tuple[str, dict[str, object]]]) -> list[str]:
    return [n for n, _ in events]


def test_starting_playback_enters_a_slide_then_starts_a_line() -> None:
    # Order matters to a player: it must know which slide before it is told
    # to play a line over it.
    ev = playback_events(Head("idle"), Head("playing", 0, "s0l0", 0), lookup())
    assert names(ev) == ["session.state", "slide.enter", "line.start", "playhead"]


def test_a_line_boundary_ends_the_old_line_before_starting_the_new_one() -> None:
    ev = names(
        playback_events(Head("playing", 0, "s0l0", 3000), Head("playing", 0, "s0l1"), lookup())
    )
    assert ev.index("line.end") < ev.index("line.start")
    assert "slide.enter" not in ev, "no slide change, so no slide.enter"


def test_crossing_a_slide_boundary_ends_the_line_before_entering_the_slide() -> None:
    # Observed live before it was fixed: `slide.enter` arrived first.
    ev = names(
        playback_events(Head("playing", 0, "s0l2", 4000), Head("playing", 1, "s1l0"), lookup())
    )
    assert ev.index("line.end") < ev.index("slide.enter") < ev.index("line.start")


def test_line_start_carries_the_speaker() -> None:
    ev = playback_events(Head("playing", 0, "a"), Head("playing", 0, "b"), lookup())
    assert dict(ev)["line.start"]["speaker_id"] == "s1"


def test_a_pause_mid_line_emits_state_and_the_exact_offset() -> None:
    ev = dict(
        playback_events(Head("playing", 1, "s1l0", 4000), Head("paused", 1, "s1l0", 4137), lookup())
    )
    assert ev["session.state"] == {"state": "paused"}
    assert ev["playhead"]["offset_ms"] == 4137, "the exact millisecond survives to the page"


def test_joining_a_fresh_session_does_not_invent_a_line_that_ended() -> None:
    # Regression: a sentinel `was` once made the first frame a `line.end` for
    # a line that never started.
    ev = names(playback_events(Head(), Head("playing", 0, "s0l0"), lookup()))
    assert "line.end" not in ev
    assert "slide.enter" in ev and "line.start" in ev


def test_nothing_changed_emits_nothing() -> None:
    # A stream that repeats itself teaches a client to ignore it.
    h = Head("playing", 0, "s0l0", 500)
    assert playback_events(h, h, lookup()) == []
