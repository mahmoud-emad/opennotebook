# ruff: noqa: E501
"""The script, ported from `opennotebook_script/src/generate.rs` and
`opennotebook_script/tests/script.rs`, plus runs of the whole script against
a stand-in model."""

import uuid
from pathlib import Path

import pytest
from httpx import AsyncClient

from opennotebook.domain.sessions import AudioSpec, Speaker
from opennotebook.script import generate as g
from opennotebook.script.errors import Empty, Truncated
from opennotebook.script.parse import ScriptedLine, parse_lines, split_slide_reply
from opennotebook.script.retrieval import Scope
from tests import model as fake
from tests.builds.fake import install as builds_fake_install
from tests.model import add_note


def line(sid: str, text: str) -> ScriptedLine:
    return ScriptedLine(sid, text)


def sp(sid: str, name: str) -> Speaker:
    return Speaker(sid, "", name, "")


def spec(*speakers: Speaker, audio: AudioSpec | None = None) -> g.ScriptSpec:
    return g.ScriptSpec("t", list(speakers), "c", "p", audio=audio)


def audio(fmt: str, focus: str = "", length: str = "default") -> AudioSpec:
    return AudioSpec(fmt, length, focus)


def test_a_speaker_named_by_display_name_is_that_speaker() -> None:
    s = spec(sp("host", "Bella"), sp("expert", "Adam"))
    raw = "Bella: Webb sees in infrared.\n**Adam**: Which goes through dust.\nhost: And far back in time."
    lines = g.lines_of(s, raw)
    assert [x.speaker_id for x in lines] == ["host", "expert", "host"]
    assert lines[1].text == "Which goes through dust."


def test_a_part_is_cut_where_it_stops_fitting_and_never_ends_on_a_question() -> None:
    lines = [
        line("adam", "Moshi treats dialogue as speech-to-speech generation."),
        line("bella", "Wait, so there's no transcription step at all?"),
        line(
            "adam",
            "None in front of the reply. Text comes along as an inner monologue, predicted "
            "just ahead of the audio it describes, which keeps the words coherent.",
        ),
        line("bella", "And that is all?"),
    ]
    # Room for the first two lines and not the answer: the question goes too.
    got = g.fit_to(lines, 120)
    assert len(got) == 1
    assert got[0].text.startswith("Moshi treats")


def test_a_speaker_saying_the_same_thing_twice_says_it_once() -> None:
    # Slide 3 of a real build on the default script model.
    lines = [
        line("bella", "How does Moshi actually improve real-time dialogue then?"),
        line(
            "adam",
            "Moshi's design is full-duplex and real-time. It removes text-based delays and "
            "handles overlapping speech without speaker turns.",
        ),
        line(
            "adam",
            "Moshi's full-duplex design means it can listen and speak simultaneously, "
            "drastically reducing latency. Unlike traditional systems, it skips text-to-speech "
            "conversion, lowering delays.",
        ),
        line("bella", "So the latency is what you feel first."),
        line("adam", "Exactly, and the codec is what makes that possible."),
    ]
    got = g.drop_restatements(lines)
    assert len(got) == 4
    assert got[1].text.startswith("Moshi's full-duplex design means")
    # Different speakers, or different content, are left alone.
    assert got[3].speaker_id == "adam"


def test_a_line_after_a_cut_is_not_kept_out_of_order() -> None:
    lines = [
        line(
            "adam",
            "A first sentence that fits. A second sentence that is far too long to be said "
            "inside what is left of this budget.",
        ),
        line("bella", "Right."),
    ]
    got = g.fit_to(lines, 60)
    assert len(got) == 1
    assert got[0].text == "A first sentence that fits."


def test_a_slide_keeps_no_more_images_than_it_may_have() -> None:
    elements = [
        "[image: a kernel scheduler] — right half",
        "Point: processes share the CPU",
        "[image: a run queue] — left",
    ]
    lines = [line("host", "The scheduler decides who runs next.")]
    one = g.shape_draft(lines, "Layout: text left, image right", elements, 650, 1)
    assert sum(1 for e in one.on_slide if e.startswith("[image:")) == 1
    assert any(e.startswith("Point:") for e in one.on_slide)
    none = g.shape_draft(lines, "", elements, 650, 0)
    assert not any(e.startswith("[image:") for e in none.on_slide)
    any_ = g.shape_draft(lines, "", elements, 650, None)
    assert sum(1 for e in any_.on_slide if e.startswith("[image:")) == 2


def test_a_stat_is_the_same_when_its_number_is() -> None:
    assert g.same_stat(
        "Stat: 512 — maximum entries in the task vector", "Stat: 512 — max task_struct entries"
    )
    assert not g.same_stat("Stat: 512 — entries", "Stat: 32 — signals")
    assert not g.same_stat("Stat:  — nothing", "Stat:  — nothing")


def test_the_prompt_offers_no_image_line_when_a_slide_may_have_none() -> None:
    s = spec()
    assert "[image:" in s.image_element()
    s.images_per_slide = 0
    assert s.image_element() == ""
    s.images_per_slide = 1
    assert "at most 1" in s.image_element()


def test_a_middle_slide_loses_its_welcome() -> None:
    # The slide-4 narration of a real run: a greeting and a promise, then
    # nothing. Neither survives; the material does.
    got = g.drop_preamble(
        [
            line("bella", "Welcome to our session on Linux processes."),
            line("bella", "Here's what you'll learn about task_struct data today."),
            line("adam", "Every process is described by a task_struct."),
        ]
    )
    assert [x.text for x in got] == ["Every process is described by a task_struct."]


def test_a_slide_of_nothing_but_greetings_is_kept_rather_than_emptied() -> None:
    assert len(g.drop_preamble([line("bella", "Welcome, everyone.")])) == 1


def test_a_slide_knows_where_it_is_and_what_came_before() -> None:
    plan = [
        g.PlanPart("Scheduling", ["each CPU has its own run queue"]),
        g.PlanPart("task_struct", []),
    ]
    d = g.Place(
        1,
        plan,
        ["adam: Each CPU runs its own scheduler."],
        ["Stat: 512 — task vector entries"],
    ).describe()
    assert "not to be shown again" in d
    assert "Stat: 512" in d
    assert "slide 2 of 2" in d
    assert "2. task_struct  <- this slide" in d
    assert "adam: Each CPU runs its own scheduler." in d
    assert "last slide" in d


def test_an_audio_prompt_has_no_slide_and_says_its_format() -> None:
    one = (
        "You write the spoken narration for one slide of an explanatory session.\n"
        "Explain THIS slide's points.\n\nThen write the line `SLIDE:` on its own, and after it "
        "the copy that appears ON the slide."
    )
    whole = (
        "The session is shown as 5 slides, one per part.\n"
        "Then the line `SLIDE:` on its own, and after it the copy. Layout. Point. No sentences, "
        "no colours or fonts.\n\nSay only what the material supports."
    )
    s = spec(audio=audio("debate", " the latency claims "))
    a = s.adapt(one)
    assert "SLIDE:" not in a and "slide's" not in a.lower(), a
    assert "one chapter of an audio overview, a Debate episode" in a, a
    assert "Format: Debate" in a and 'Never say "deep dive"' in a
    assert '"the latency claims"' in a
    w = s.adapt(whole)
    assert "SLIDE:" not in w and "Layout" not in w, w
    assert "is heard as 5 chapters" in w and "Say only what" in w, w
    # A slide session is not touched.
    plain = spec()
    assert plain.adapt(one) == one
    assert plain.adapt_user(whole) == whole
    assert g.outline_shape(plain) == ""
    assert "weakness" in g.outline_shape(spec(audio=audio("critique")))


@pytest.mark.parametrize("fmt", ["deep_dive", "brief", "critique", "debate"])
def test_every_format_has_its_own_rules(fmt: str) -> None:
    a = audio(fmt)
    assert f"Format: {a.label}" in g.format_rules(a)


def test_a_length_the_format_does_not_offer_becomes_default() -> None:
    assert audio("debate", length="longer").real_length() == "default"
    assert audio("brief", length="longer").minutes() == 2


def test_decoration_in_a_plan_is_not_a_part() -> None:
    raw = "Debate: Moshi\n\nPART ONE\nThe problem\n- latency\nThe answer\n- speech in, speech out\n"
    assert [p.title for p in g.parse_plan(raw, 3)] == ["The problem", "The answer"]
    assert g.is_label("PART ONE") and g.is_label("Section 3") and not g.is_label("Part of speech")
    long = "The problem Moshi solves and why text might not be the interface we need"
    assert g.fit_title(long) == "The problem Moshi solves and why text might not"


def test_a_debate_has_sides_and_a_critique_has_a_reviewer() -> None:
    for fmt, want, unwanted in [
        ("debate", "Bella argues one side", "listener's stand-in: sharp"),
        ("critique", "Bella is the reviewer", "listener's stand-in: sharp"),
        ("deep_dive", "listener's stand-in: sharp", "argues one side"),
    ]:
        d = g.dialogue(spec(sp("bella", "Bella"), sp("adam", "Adam"), audio=audio(fmt)))
        assert want in d and unwanted not in d, (fmt, d)


def test_a_plan_gives_each_part_its_own_points() -> None:
    raw = (
        "**Slide 1: Why spoken dialogue is hard**\n"
        "- Pipelines of ASR, LLM and TTS add seconds of latency\n"
        "- Text loses tone and emotion\n"
        "2. Moshi's answer\n"
        "* Speech in, speech out, one model\n"
        "* 200 ms in practice\n"
        "Inner Monologue\n"
        "Extra part beyond the count\n"
    )
    plan = g.parse_plan(raw, 3)
    assert len(plan) == 3
    assert plan[0].title == "Why spoken dialogue is hard"
    assert len(plan[0].points) == 2
    assert plan[1].title == "Moshi's answer"
    assert plan[1].points[1] == "200 ms in practice"
    assert plan[2].points == [], "a bare title is still a part"
    assert "200 ms" in plan[1].query(), "retrieval follows the points"
    listing = g.plan_listing(plan, 1)
    assert "2. Moshi's answer  <- this slide" in listing
    assert "- Text loses tone and emotion" in listing


def test_two_speakers_are_a_host_and_an_explainer_and_one_speaks_to_you() -> None:
    s = spec(sp("bella", "Bella"), sp("adam", "Adam"))
    two = g.dialogue(s)
    assert "Bella leads" in two and "Adam explains" in two
    assert "answered in the very next line" in two
    s.speakers = s.speakers[:1]
    assert '"you"' in g.dialogue(s)


def test_an_edit_that_guts_a_part_is_not_taken() -> None:
    # Plan §9: the edit pass is accepted only if it parses, keeps the
    # speakers and loses under 40%.
    draft = [
        line("bella", "So the model hears and speaks at the same time?"),
        line("adam", "Yes, it models both streams in parallel, which is how it handles overlap."),
    ]
    assert g.keeps_the_part(draft, draft)
    assert not g.keeps_the_part(draft, draft[1:]), "a monologue where there was a conversation"
    assert not g.keeps_the_part(draft, [line("adam", "Yes.")])
    assert not g.keeps_the_part(draft, [])
    # Two fifths is the line: 60% kept passes, under it does not.
    long = [line("a", "x" * 100), line("b", "y" * 100)]
    assert g.keeps_the_part(long, [line("a", "x" * 60), line("b", "y" * 60)])
    assert not g.keeps_the_part(long, [line("a", "x" * 59), line("b", "y" * 60)])


def test_a_whole_session_reply_is_cut_at_its_parts() -> None:
    raw = (
        "Sure, here is the script.\n"
        "=== PART 1 ===\n"
        "bella: Welcome.\n"
        "SLIDE:\n"
        "Layout: text only\n"
        "Point: one\n"
        "## Part 2 — Scheduling\n"
        "adam: And each CPU has its own queue.\n"
        "**Slide 3**\n"
        "bella: Which brings us to task_struct.\n"
    )
    parts = g.split_parts(raw, 4)
    assert len(parts) == 4
    one = parts[0]
    assert one is not None and one.startswith("bella: Welcome.")
    assert "SLIDE:" in one, "the copy marker stays inside its part"
    assert parts[1] == "adam: And each CPU has its own queue.\n"
    assert parts[2] is not None
    assert parts[3] is None, "a part never written is left to the fallback"


def test_speech_after_the_slide_copy_is_still_speech() -> None:
    ids = ["host", "expert"]
    part = (
        "host: Welcome.\nSLIDE:\nLayout: text only\nPoint: states\nhost: Processes change state.\n"
    )
    spoken, elements = split_slide_reply(g.speech_first(part, ids))
    lines = parse_lines(spoken, ids)
    assert len(lines) == 2
    assert lines[1].text == "Processes change state."
    assert len(elements) == 1


def test_an_outline_title_loses_its_slide_label() -> None:
    assert g.strip_slide_label("Slide 1: Process Isolation") == "Process Isolation"
    assert g.strip_slide_label("2. The task_struct") == "The task_struct"
    assert g.strip_slide_label("Symmetric multiprocessing") == "Symmetric multiprocessing"
    assert g.strip_slide_label("64-bit address spaces") == "64-bit address spaces"
    assert g.strip_slide_label("3) Zombies") == "Zombies"
    assert g.strip_slide_label("Part 1: The Foundation of Moshi") == "The Foundation of Moshi"
    assert g.strip_slide_label("Chapter 2 - Mimi") == "Mimi"
    assert g.strip_slide_label("Particle physics") == "Particle physics"


def test_the_copy_marker_is_not_a_part_marker() -> None:
    assert g.part_marker("SLIDE:") is None
    assert g.part_marker("Slide: Layout: text only") is None
    assert g.part_marker("=== PART 3 ===") == 3
    assert g.part_marker("PART 12:") == 12


def test_a_closing_line_on_the_last_slide_is_kept() -> None:
    lines = [
        line("bella", "task_struct holds a process's state."),
        line("bella", "Today, we covered scheduling and task_struct."),
    ]
    assert len(g.drop_preamble(lines)) == 2


def test_a_slug_is_a_safe_snake_case_name() -> None:
    assert g.slug("Each Slide Has Spoken Words", 0) == "each_slide_has_spoken_words"
    assert g.slug("Who holds the floor?", 1) == "who_holds_the_floor"
    assert g.slug("!!!", 2) == "slide_2", "a title with no word characters still names a slide"


# ── against a model ──────────────────────────────────────────────────────────


async def _collection(client: AsyncClient) -> tuple[uuid.UUID, uuid.UUID]:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    owner = (await client.get("/api/me")).json()["id"]
    return uuid.UUID(owner), uuid.UUID(cid)


async def test_an_empty_collection_refuses_rather_than_inventing(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Plan §9: generating against a collection with nothing in it must refuse
    # rather than let the model write from its own knowledge — before any
    # model call.
    model = fake.install(monkeypatch)
    owner, cid = await _collection(client)
    with pytest.raises(g.NoGrounding) as e:
        await g.generate_script(Scope(owner, cid), spec(sp("narrator", "Narrator")))
    assert model.bodies == []
    assert e.value.sentence.endswith(".")


async def test_truncation_is_detected_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    # Plan §9: a reply cut at the model's ceiling reads whole; the finish
    # reason is what shows it. The outline refuses it; a slide keeps it.
    model = fake.install(monkeypatch)
    model.answers(fake.says("Part one\n- a point", finish="length"))
    with pytest.raises(Truncated) as e:
        await g.send("m/x", "system", "user", "outline")
    assert e.value.text.startswith("Part one")
    model.answers(fake.says("host: kept before the cut.", finish="max_tokens"))
    s = spec(sp("host", "Host"))
    assert await g.complete_partial(s, "system", "user", "slide script") == (
        "host: kept before the cut."
    )
    model.answers(fake.says("   "))
    with pytest.raises(Empty):
        await g.send("m/x", "system", "user", "outline")


async def test_a_whole_script_is_written_from_the_sources(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    studio, _ = builds_fake_install(monkeypatch, tmp_path)
    owner, cid = await _collection(client)
    await add_note(client, str(cid), "Coral reefs are built by polyps over centuries. " * 20)
    s = spec(sp("host", "Bella"), sp("expert", "Adam"))
    s.slide_count = 3
    parts = await g.generate_script(Scope(owner, cid), s)
    assert [p.title for p in parts] == [f"Part title number {i}" for i in (1, 2, 3)]
    for p in parts:
        assert p.lines and all(x.line_id == f"s{p.ordinal}l{n}" for n, x in enumerate(p.lines))
        assert {x.speaker_id for x in p.lines} <= {"host", "expert"}
        assert p.on_slide[0].startswith("Layout: ")
        assert sum(len(x.text) for x in p.lines) <= s.slide_narration + 3 * 320
    # One outline, one whole-session script, one close, one edit a part.
    assert studio.asked.count("outline") == 1
    assert studio.asked.count("session") == 1
    assert studio.asked.count("edit") == 3
    assert studio.asked.count("bookend") == 1
    # The close is the last thing said.
    assert (
        parts[-1]
        .lines[-1]
        .text.startswith("And that detail matters because it explains the session")
    )
