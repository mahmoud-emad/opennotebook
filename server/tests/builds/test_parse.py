"""Parsing the script model's output, ported from
`opennotebook_script/src/parse.rs`."""

import pytest

from opennotebook.script.parse import (
    UnknownSpeaker,
    layout_of,
    parse_lines,
    resolve,
    split_slide_reply,
)

KNOWN = ["host", "expert"]


def test_two_speakers_alternating_parse_in_order() -> None:
    lines = parse_lines(
        "host: What is a narrated session?\nexpert: Slides with a spoken script.\n", KNOWN
    )
    assert len(lines) == 2
    assert lines[0].speaker_id == "host"
    assert lines[1].text == "Slides with a spoken script."


def test_one_speaker_uses_the_same_parser() -> None:
    # A one-speaker session goes through the same parser with a one-element
    # speaker set. No branch, no special case.
    assert len(parse_lines("narrator: A single voice.\n", ["narrator"])) == 1


def test_blank_lines_and_comments_are_skipped_but_a_stray_speaker_is_not() -> None:
    assert len(parse_lines("# a heading\n\nhost: Real.\n", KNOWN)) == 1
    with pytest.raises(UnknownSpeaker) as e:
        parse_lines("narrator: Not in this session.\n", KNOWN)
    assert e.value.line == 1
    assert e.value.sentence.endswith(".")


@pytest.mark.parametrize(
    "raw",
    [
        "host_id: A single voice.\n",
        "**host**: A single voice.\n",
        "Host: A single voice.\n",
        "host id: A single voice.\n",
    ],
)
def test_a_known_speaker_wearing_punctuation_or_the_word_id_is_still_that_speaker(
    raw: str,
) -> None:
    # The failure that killed a real prep: one speaker named `host`, every
    # line tagged `host_id`, five minutes of work thrown away over a spelling.
    lines = parse_lines(raw, ["host"])
    assert lines[0].speaker_id == "host"
    assert lines[0].text == "A single voice."


def test_somebody_else_is_still_an_error() -> None:
    with pytest.raises(UnknownSpeaker):
        parse_lines("expert: Not here.\n", ["host"])


def test_only_the_first_colon_separates() -> None:
    lines = parse_lines("host: The rule is this: read the discriminator.", KNOWN)
    assert lines[0].text == "The rule is this: read the discriminator."


REPLY = (
    "host: Attention lets a token look at every other token.\n"
    "expert: And the weights are learned, not fixed.\n"
    "SLIDE:\n"
    "Layout: left text / right image\n"
    "- Point: query, key, value\n"
    "- Subhead: learned, not fixed\n"
    "- [image: three vectors meeting at a dot product] — right half\n"
)


def test_the_spoken_half_and_the_printed_half_come_apart() -> None:
    spoken, elements = split_slide_reply(REPLY)
    assert "host: Attention lets" in spoken
    assert "expert: And the weights" in spoken
    assert "SLIDE" not in spoken, "the marker leaked"
    assert "Point:" not in spoken, "slide copy leaked"
    assert len(elements) == 3
    assert elements[0] == "Point: query, key, value"
    assert elements[2].startswith("[image:")


def test_the_layout_line_is_not_an_element() -> None:
    _, elements = split_slide_reply(REPLY)
    assert not any(e.startswith("Layout") for e in elements)
    assert layout_of(REPLY) == "left text / right image"


def test_an_invented_layout_is_refused() -> None:
    assert layout_of("SLIDE:\nLayout: dramatic diagonal swoosh\n") is None
    # Case and spacing are the model's, not the spec's.
    assert layout_of("Layout:   TWO COLUMNS  \n") == "two columns"


def test_an_unknown_element_label_is_dropped() -> None:
    _, elements = split_slide_reply(
        "SLIDE:\nVisual direction: something painterly\n- Point: kept\n"
    )
    assert elements == ["Point: kept"]


def test_a_reply_with_no_marker_is_all_narration() -> None:
    spoken, elements = split_slide_reply("host: just talking\n")
    assert "host: just talking" in spoken
    assert elements == []


@pytest.mark.parametrize(
    "marker", ["SLIDE:", "slide", "**SLIDE:**", "## SLIDE", "- SLIDE:", "ON SLIDE:", "  slide:  "]
)
def test_the_marker_is_recognised_however_it_is_dressed(marker: str) -> None:
    # A marker that is not recognised stays in the spoken half and is then
    # parsed as a speaker line: "`SLIDE` is not a speaker of this session".
    spoken, elements = split_slide_reply(
        f"host: spoken\n{marker}\nLayout: two columns\n- Point: kept\n"
    )
    assert "slide" not in spoken.lower()
    assert elements == ["Point: kept"]


def test_a_marker_carrying_its_first_element_keeps_it() -> None:
    spoken, elements = split_slide_reply("host: spoken\nSLIDE: Point: inline\n")
    assert "host: spoken" in spoken
    assert elements == ["Point: inline"]


def test_narration_about_slides_is_not_the_marker() -> None:
    spoken, elements = split_slide_reply(
        "host: the next slide shows the encoder\nSLIDE:\n- Point: k\n"
    )
    assert "the next slide shows" in spoken
    assert elements == ["Point: k"]


def test_stray_copy_never_reaches_the_speaker_parser() -> None:
    spoken, elements = split_slide_reply(
        "host: spoken line\nLayout: two columns\n- Point: stray\nhost: another\n"
    )
    assert "Point:" not in spoken and "Layout:" not in spoken
    assert "host: spoken line" in spoken and "host: another" in spoken
    assert elements == ["Point: stray"]


def test_a_colon_inside_narration_is_not_an_element() -> None:
    spoken, elements = split_slide_reply("host: the rule is simple: attend to everything\n")
    assert "attend to everything" in spoken
    assert elements == []


def test_a_reply_with_only_slide_copy_leaves_no_narration() -> None:
    raw = (
        "SLIDE: Listing running processes with ps\n"
        "Layout: centered title\n"
        "Point: ps command\n"
        "Subhead: snapshot of current processes\n"
    )
    spoken, elements = split_slide_reply(raw)
    assert spoken.strip() == ""
    assert len(elements) == 2
    assert layout_of(raw) == "centered title"


def test_the_models_usual_shape_keeps_its_narration() -> None:
    raw = (
        "host: Let's explore the ps command for listing running processes.\n\n"
        "SLIDE: Listing running processes with ps\n\n"
        "Layout: centered title\n\n"
        "Point: ps command\n"
    )
    spoken, elements = split_slide_reply(raw)
    assert "host: Let's explore" in spoken
    assert elements == ["Point: ps command"]


@pytest.mark.parametrize(
    ("tag", "want"),
    [
        ("A", "host"),
        ("B", "expert"),
        ("a", "host"),
        ("b", "expert"),
        ("1", "host"),
        ("2", "expert"),
        ("Speaker 1", "host"),
        ("speaker2", "expert"),
        ("S1", "host"),
        ("s2", "expert"),
    ],
)
def test_a_speaker_named_by_position_resolves(tag: str, want: str) -> None:
    assert resolve(tag, KNOWN) == want


def test_the_real_ids_still_resolve_first() -> None:
    assert resolve("host", KNOWN) == "host"
    assert resolve("expert", KNOWN) == "expert"
    assert resolve("HOST", KNOWN) == "host"


def test_a_position_past_the_roster_is_still_unknown() -> None:
    assert resolve("B", ["host"]) is None
    assert resolve("3", KNOWN) is None


def test_a_meaningless_tag_still_resolves_to_nobody() -> None:
    assert resolve("Narrator", KNOWN) is None
    assert resolve("Note", KNOWN) is None
