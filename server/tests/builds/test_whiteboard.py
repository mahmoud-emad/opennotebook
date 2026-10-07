"""Whiteboard videos (docs/video-overview-spec.md, phase 2): the scene
schema, the icons, the compiler's placing and timing, the lint, the
writer's plan and scenes with their repair and plain fallbacks, the frames,
and the render through the API against a stand-in model.

Renders that need ffmpeg are skipped where it is missing."""

import hashlib
import itertools
import shutil
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from pydantic import ValidationError

from opennotebook.build import video
from opennotebook.build.whiteboard import check, draw, geometry, ground, icons, lint, write
from opennotebook.build.whiteboard.compile import SAFE, SETTLE_MS, Drawing, compile_scene
from opennotebook.build.whiteboard.scene import Beat, Element, Plan, PlannedScene, Scene
from opennotebook.domain.sessions import Line, Part
from tests.builds.conftest import drain
from tests.builds.fake import install
from tests.model import add_note

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg"
)


@pytest.fixture
def files(tmp_path: Path) -> Path:
    return tmp_path


def _when(b: Beat) -> float:
    """Line `l<n>` starts at n seconds; each word is 300 ms."""
    return int(b.line[1:]) * 1000 + b.word * 300.0


def _scene(**over: Any) -> Scene:
    v: dict[str, Any] = {
        "title": "The kernel",
        "elements": [
            {"id": "cpu", "kind": "icon", "icon": "cpu", "at": "A2", "span": [2, 3],
             "label": "processor", "tone": "blue", "beat": {"line": "l0", "word": 1}},
            {"id": "k", "kind": "box", "at": "D2", "span": [3, 3], "label": "kernel",
             "beat": {"line": "l0", "word": 3}},
            {"id": "a", "kind": "arrow", "from": "k", "to": "cpu", "label": "runs",
             "beat": {"line": "l1", "word": 0}},
        ],
        "highlight": [{"target": "k", "beat": {"line": "l1", "word": 2}}],
    }  # fmt: skip
    v.update(over)
    return Scene.model_validate(v)


# ── the schema ───────────────────────────────────────────────────────────────


def test_a_scene_names_cells_short_labels_and_joins() -> None:
    sc = _scene()
    assert sc.elements[2].source == "k" and sc.elements[2].target == "cpu"
    long = Element.model_validate(
        {"kind": "label", "at": "a1", "label": "a very long label " * 5, "beat": {"line": "l0"}}
    )
    assert long.at == "A1" and len(long.label) <= 28
    for bad in ({"at": "G1"}, {"at": "A7"}, {"span": [7, 1]}):
        with pytest.raises(ValidationError):
            Element.model_validate({"kind": "box", "at": "A1", "beat": {"line": "l0"}, **bad})


# ── icons and geometry ───────────────────────────────────────────────────────


def test_icons_are_found_by_what_they_show() -> None:
    assert icons.search("web server")[0] == "server"
    assert "building" in icons.search("companies")
    assert icons.search("the of and") == []
    assert icons.exists("cpu") and not icons.exists("tux-penguin")
    b = icons.path("cpu").getBounds()
    assert b.left() >= 0 and b.right() <= icons.SIZE


def test_a_stroke_wobbles_but_its_ends_stay_put() -> None:
    line = [(float(x), 100.0) for x in range(0, 400, 4)]
    w = geometry.wobble(line, seed=7)
    assert w[0] == line[0] and w[-1] == pytest.approx(line[-1])
    assert 0.2 < max(abs(y - 100) for _, y in w) <= 1.3
    assert geometry.wobble(line, seed=7) == w, "the same seed draws the same way"


def test_text_is_outlines_with_a_width() -> None:
    t = geometry.text("Kernel", 60)
    assert t.width > 100 and t.ascent > 0
    assert not t.path.getBounds().isEmpty()


# ── the compiler ─────────────────────────────────────────────────────────────


def _inside(inner: tuple[float, ...], outer: tuple[float, ...]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] + 1


def test_every_element_stays_in_its_cells_and_arrows_join_drawings() -> None:
    d = compile_scene(_scene(), _when, 0, 6000)
    for p in d.pieces:
        if p.element in d.boxes:
            xs = [x for x, _ in p.points] or [p.box[0], p.box[2]]
            ys = [y for _, y in p.points] or [p.box[1], p.box[3]]
            assert _inside((min(xs), min(ys), max(xs), max(ys)), d.boxes[p.element]), p.element
    assert lint.problems(d) == []


def test_neighbours_still_get_an_arrow_you_can_see() -> None:
    sc = _scene(
        elements=[
            {"id": "a", "kind": "icon", "icon": "server", "at": "A1", "span": [2, 2],
             "beat": {"line": "l0"}},
            {"id": "b", "kind": "icon", "icon": "cpu", "at": "C3", "span": [2, 2],
             "beat": {"line": "l0"}},
            {"id": "x", "kind": "arrow", "from": "a", "to": "b", "beat": {"line": "l0"}},
        ],
        highlight=[],
    )  # fmt: skip
    d = compile_scene(sc, _when, 0, 6000)
    shaft = next(p for p in d.pieces if p.element == "x")
    assert geometry.length_of(shaft.points) > lint.MIN_ARROW
    # Side by side, with no room between them: drawn at its shortest anyway.
    side = _scene(
        elements=[
            {"id": "a", "kind": "box", "at": "A1", "span": [2, 2], "label": "one",
             "beat": {"line": "l0"}},
            {"id": "b", "kind": "box", "at": "C1", "span": [2, 2], "label": "two",
             "beat": {"line": "l0"}},
            {"id": "x", "kind": "arrow", "from": "a", "to": "b", "beat": {"line": "l0"}},
        ],
        highlight=[],
    )  # fmt: skip
    d = compile_scene(side, _when, 0, 6000)
    shaft = next(p for p in d.pieces if p.element == "x")
    assert geometry.length_of(shaft.points) >= 69
    assert not [p for p in lint.problems(d) if "arrow" in p]


def test_things_are_drawn_on_their_word_one_at_a_time_and_finish_in_time() -> None:
    d = compile_scene(_scene(), _when, 0, 6000)
    first = {p.element: p.start_ms for p in reversed(d.pieces)}
    assert first["cpu"] >= _when(Beat(line="l0", word=1)) - 120
    assert first["k"] >= _when(Beat(line="l0", word=3)) - 120
    assert first["a"] >= 1000 - 120
    ordered = sorted(d.pieces, key=lambda p: p.start_ms)
    assert all(a.end_ms <= b.start_ms + 1e-6 for a, b in itertools.pairwise(ordered))
    assert max(p.end_ms for p in d.pieces) <= 6000 - SETTLE_MS


def test_a_scene_whose_words_come_late_is_drawn_faster_inside_it() -> None:
    late = _scene(highlight=[])
    for e in late.elements:
        e.beat = Beat(line="l1", word=6)
    d = compile_scene(late, _when, 0, 3000)
    assert max(p.end_ms for p in d.pieces) <= 3000 - SETTLE_MS + 1
    assert min(p.start_ms for p in d.pieces) >= 0


def test_an_unknown_icon_is_replaced_or_drawn_as_a_box_and_said() -> None:
    sc = _scene(
        elements=[
            {"id": "a", "kind": "icon", "icon": "servers-rack", "label": "server", "at": "A2",
             "span": [2, 2], "beat": {"line": "l0"}},
            {"id": "b", "kind": "icon", "icon": "zzqx", "label": "qqzx", "at": "D2",
             "span": [2, 2], "beat": {"line": "l0"}},
        ],
        highlight=[],
    )  # fmt: skip
    d = compile_scene(sc, _when, 0, 6000)
    assert any("drew 'server" in n for n in d.notes)
    assert any("drew a box" in n for n in d.notes)


def test_odd_scenes_are_drawn_or_said_never_crash() -> None:
    sc = _scene(
        elements=[
            {"kind": "box", "at": "A1", "span": [2, 2], "label": "unnamed",
             "beat": {"line": "l0"}},
            {"id": "e0", "kind": "box", "at": "D1", "span": [2, 2], "label": "named e0",
             "beat": {"line": "l0"}},
            {"id": "x", "kind": "arrow", "from": "e0", "to": "e0", "beat": {"line": "l0"}},
            {"id": "s", "kind": "sketch", "at": "A4", "span": [2, 2], "d": "M0 0 L1e38 1e38 Z",
             "beat": {"line": "l0"}},
        ],
        highlight=[],
    )  # fmt: skip
    assert "joins 'e0' to itself" in " ".join(write.scene_problems(sc, {"l0": 6}))
    d = compile_scene(sc, _when, 0, 6000)
    assert len(d.boxes) == 3, "an element given an id does not take another's"
    assert any("to itself; left out" in n for n in d.notes)


def test_a_letter_the_handwriting_lacks_comes_from_another_font_or_is_said() -> None:
    arabic = geometry.text("العربية", 60)
    assert arabic.missing, "a script that needs shaping is not drawn letter by letter"
    if geometry.fallback("内") is not None:
        assert geometry.text("内核", 60).missing == ()


# ── the lint ─────────────────────────────────────────────────────────────────


def _problems(**over: Any) -> list[str]:
    return lint.problems(compile_scene(_scene(**over), _when, 0, 6000))


def test_the_lint_names_what_to_fix() -> None:
    shared = _problems(
        elements=[
            {"id": "a", "kind": "box", "at": "A1", "span": [3, 3], "label": "one",
             "beat": {"line": "l0"}},
            {"id": "b", "kind": "box", "at": "B2", "span": [3, 3], "label": "two",
             "beat": {"line": "l0"}},
        ],
        highlight=[],
    )  # fmt: skip
    assert any("a and b are in the same cells" in p for p in shared)
    sparse = _problems(
        elements=[{"id": "a", "kind": "label", "at": "A1", "label": "hi", "beat": {"line": "l0"}}],
        highlight=[],
    )
    assert any("fills only" in p for p in sparse)
    left_out = _problems(
        elements=[
            {"id": "a", "kind": "box", "at": "A1", "span": [6, 6], "label": "x",
             "beat": {"line": "l0"}},
            {"id": "z", "kind": "arrow", "from": "a", "to": "ghost", "beat": {"line": "l0"}},
        ],
        highlight=[],
    )  # fmt: skip
    assert any("left out" in p for p in left_out)


def test_a_label_crossed_by_another_line_is_found() -> None:
    d = compile_scene(_scene(), _when, 0, 6000)
    label = next(p for p in d.pieces if p.kind == "text" and p.element == "k")
    x0, y0, x1, y1 = label.box
    across = [(x0 - 20 + i * 4.0, (y0 + y1) / 2) for i in range(int((x1 - x0 + 40) / 4))]
    d.pieces.append(type(label)("intruder", "line", (0, 0, 0), points=across))
    assert any("crossed by intruder" in p for p in lint.problems(d))


# ── the writer ───────────────────────────────────────────────────────────────


def _parts() -> list[Part]:
    return [
        Part("a", 0, "Intro", ["Point: kernel", "Point: memory"],
             [Line("l0", "host", 0, "The kernel manages memory and disks.", "x", 3000),
              Line("l1", "host", 1, "It runs every program you use.", "x", 2500)]),
        Part("b", 1, "Distros", [], [Line("l2", "host", 0, "Ubuntu is one of them.", "x", 2000)]),
    ]  # fmt: skip


def test_a_plan_must_cover_the_narration_in_order() -> None:
    order = ["l0", "l1", "l2"]
    good = Plan(scenes=[PlannedScene(lines=["l0", "l1"]), PlannedScene(lines=["l2"])])
    assert write.plan_problems(good, order) == []
    for bad in (["l0"], ["l1", "l0", "l2"], ["l0", "l1", "l1", "l2"]):
        assert write.plan_problems(Plan(scenes=[PlannedScene(lines=bad)]), order)
    fallback = write.default_plan(_parts())
    assert [s.lines for s in fallback.scenes] == [["l0", "l1"], ["l2"]]
    assert fallback.scenes[0].concepts == ["kernel", "memory"]


def test_a_scene_is_checked_before_it_is_drawn() -> None:
    lines = {"l0": 6, "l1": 6}
    assert write.scene_problems(_scene(), lines) == []
    bad = _scene(
        elements=[
            {"id": "a", "kind": "icon", "icon": "no-such-icon", "at": "A1",
             "beat": {"line": "l9", "word": 0}},
            {"id": "a", "kind": "box", "at": "C1", "beat": {"line": "l0", "word": 99}},
            {"id": "j", "kind": "arrow", "from": "a", "to": "q", "beat": {"line": "l0"}},
        ],
        highlight=[],
    )  # fmt: skip
    said = " | ".join(write.scene_problems(bad, lines))
    assert "share an id" in said and "not one of this scene's" in said
    assert "word 99 of l0, which has 6" in said and "not in the scene" in said
    assert "not in the library" in said
    empty = _scene(
        elements=[{"id": "b", "kind": "box", "at": "A1", "label": " ", "beat": {"line": "l0"}}],
        highlight=[],
    )
    assert write.scene_problems(empty, lines) == ["box b has no label: name what it stands for"]


def test_a_plain_scene_is_drawable_and_clean() -> None:
    ps = PlannedScene(lines=["l0", "l1"], title="Intro", concepts=["kernel", "memory", "disk"])
    words = {
        "l0": ["The", "kernel", "manages", "memory"],
        "l1": ["and", "every", "disk", "you", "have"],
    }
    sc = write.plain_scene(ps, words, icons.candidates(ps.concepts))
    assert lint.problems(compile_scene(sc, _when, 0, 6000)) == []
    beats = {e.label: (e.beat.line, e.beat.word) for e in sc.elements if e.label}
    assert beats == {"kernel": ("l0", 1), "memory": ("l0", 3), "disk": ("l1", 2)}
    assert sum(e.kind == "arrow" for e in sc.elements) == 2, "three concepts, joined"


def test_the_scene_prompt_numbers_the_words_the_beats_count() -> None:
    p = write._scene_prompt(  # pyright: ignore[reportPrivateUsage]
        PlannedScene(lines=["l0"], concepts=["kernel"]), _parts(), {"kernel": ["cpu"]}
    )
    assert "l0: [0]The [1]kernel [2]manages" in p and "- kernel: cpu (Devices)" in p


# ── accuracy ─────────────────────────────────────────────────────────────────


def test_every_word_on_the_board_must_have_been_said() -> None:
    said = ground.Said.of(
        ["Plants turn sunlight and water into glucose.", "About 75% of 1,000 leaves grow."]
    )
    assert said.unsaid("Leaves") == [] and said.unsaid("sunlight + water") == []
    assert said.unsaid("75 percent") == [] and said.unsaid("1000") == []
    assert said.unsaid("glucose energy") == ["energy"] and said.unsaid("80%") == ["80"]
    sc = _scene(
        title="How plants eat",
        elements=[
            {"id": "a", "kind": "box", "at": "A1", "label": "glucose", "beat": {"line": "l0"}},
            {"id": "b", "kind": "number", "at": "C1", "text": "90%", "label": "leaves",
             "beat": {"line": "l0"}},
        ],
        highlight=[],
    )  # fmt: skip
    found = ground.problems(sc, said)
    # The title is a heading, not held to the narration's words.
    assert len(found) == 1 and "'90'" in found[0]
    assert ground.counts(sc, said) == (3, 2)


def test_a_plain_scene_writes_only_what_was_said() -> None:
    words = {"l0": ["The", "kernel", "manages", "memory."]}
    said = ground.Said.of(["The kernel manages memory."])
    ps = PlannedScene(lines=["l0"], title="Invented title", concepts=["kernel", "quantum foam"])
    sc = write.plain_scene(ps, words, {}, said)
    assert ground.problems(sc, said) == []
    assert [e.label for e in sc.elements if e.label] == ["kernel"] and sc.title == ""


def test_the_checkers_answer_becomes_repairs() -> None:
    ok = check.judged(
        {"claims": [{"claim": "a", "supported": True}], "recognizable": True,
         "matches_narration": True},
        1,
    )  # fmt: skip
    assert ok.problems == [] and (ok.claims, ok.supported) == (1, 1)
    bad = check.judged(
        {"claims": [{"claim": "the kernel is a CPU", "supported": False, "why": "it is not"}],
         "missing": ["memory"], "unreadable": ["kernel"], "matches_narration": False,
         "fixes": ["draw memory"]},
        2,
    )  # fmt: skip
    said = " | ".join(bad.problems)
    assert "'the kernel is a CPU' is not supported: it is not" in said
    assert "fix: draw memory" in said and (bad.claims, bad.supported) == (2, 0)
    # What the picture looks like it gets wrong is advice, not a fault.
    advice = " | ".join(bad.advice)
    assert "memory is not recognisable" in advice and "kernel cannot be read" in advice
    seen = check.judged({"claims": [], "unreadable": ["kernel"], "fixes": ["bigger"]}, 0)
    assert seen.problems == [] and seen.advice == ["kernel cannot be read", "fix: bigger"]


def test_a_scene_says_what_its_arrows_claim_and_is_seen_as_a_picture() -> None:
    sc = _scene(claims=[{"text": "The kernel runs the processor", "passages": ["p1"]}])
    assert check.relations(sc) == ["The kernel runs the processor", "kernel runs processor"]
    png = check.still(sc, _when, 0, 6000)
    assert png.startswith(b"\x89PNG")


# ── the presenter ────────────────────────────────────────────────────────────


def test_fragments_become_whole_sentences_and_paragraphs() -> None:
    from opennotebook.build.whiteboard import presenter

    text = presenter.joined(
        ["Have you ever wondered how a tree grows so big", "without ever eating anything",
         "Plants don't eat food the way we do.", "instead, they make their own food"]
    )  # fmt: skip
    assert text == (
        "Have you ever wondered how a tree grows so big, without ever eating anything. "
        "Plants don't eat food the way we do. Instead, they make their own food."
    )
    paras = presenter.paragraphs(text + " " + "It is simple. " * 4)
    assert all(len(presenter._sentences(p)) <= 3 for p in paras)  # pyright: ignore[reportPrivateUsage]
    assert " ".join(paras).split() == (text + " " + "It is simple. " * 4).split()


def test_a_rewrite_may_not_add_a_fact_drop_a_part_or_balloon() -> None:
    from opennotebook.build.whiteboard import presenter

    parts = _parts()
    good = [["So, the kernel manages memory and disks. It runs every program you use."],
            ["Ubuntu is one of them."]]  # fmt: skip
    assert presenter.problems(parts, good, []) == []
    invented = [["The kernel, written in 1991 by Linus, manages memory and disks for you."],
                ["Ubuntu is one of them."]]  # fmt: skip
    said = " ".join(presenter.problems(parts, invented, []))
    assert "'1991'" in said and "'Linus'" in said
    assert presenter.problems(parts, good[:1], []) == ["there are 1 parts, not 2"]
    long = [[good[0][0] * 4], good[1]]
    assert "times the script's length" in presenter.problems(parts, long, [])[0]


def test_the_opening_closing_and_slides_say_only_what_the_script_does() -> None:
    from opennotebook.build.whiteboard import presenter

    parts = _parts()
    body = [["The kernel manages memory and disks."], ["Ubuntu is one of them."]]
    ok = presenter.problems(parts, body, [], ["Here's how it works."], ["That's it."], [])
    assert ok == []
    bad = presenter.problems(
        parts, body, [], ["Since 1991, it has run everything."], ["That's it."], ["Made by Linus"]
    )
    assert any("the opening" in p and "'1991'" in p for p in bad)
    assert any("the slides" in p and "'Linus'" in p for p in bad)
    # An agenda item too long for the slide is cut to whole words.
    assert presenter.short("How the immune system learns from a vaccine", 24) == (
        "How the immune system"
    )


# ── the frames ───────────────────────────────────────────────────────────────


def _segment(tmp: Path, frames: int = 45) -> draw.Segment:
    return draw.Segment(
        scene=_scene().model_dump(mode="json", by_alias=True),
        words={"l0": [i * 300.0 for i in range(6)], "l1": [1000 + i * 300.0 for i in range(6)]},
        line_starts={"l0": 0.0, "l1": 1000.0},
        start_ms=0,
        end_ms=frames * 1000 / draw.FPS,
        first_frame=0,
        frames=frames,
        wipe=False,
        out=str(tmp / "seg.mp4"),
        ffmpeg=shutil.which("ffmpeg") or "ffmpeg",
    )


def test_frames_start_blank_fill_in_and_are_the_same_every_time(tmp_path: Path) -> None:
    seg = _segment(tmp_path)
    a = [hashlib.blake2b(f, digest_size=8).digest() for f, _ in draw.frames(seg)]
    b = [hashlib.blake2b(f, digest_size=8).digest() for f, _ in draw.frames(seg)]
    assert a == b, "a render is deterministic"
    assert len(set(a)) > 10, "the board changes as it is drawn"
    first = next(iter(draw.frames(seg)))[0]
    assert len(first) == 1920 * 1080 * 4
    assert set(first[:4000]) <= set(draw.BOARD) | {255}, "the first frame is the empty board"


@needs_ffmpeg
@pytest.mark.parametrize("theme", ["whiteboard", "chalkboard", "papercraft"])
def test_a_segment_is_drawn_in_pieces_that_join_to_every_frame(tmp_path: Path, theme: str) -> None:
    """A scene that finishes drawing early holds still for the rest: the
    hold is a piece of its own, and the pieces joined have every frame. In
    chalk too, whose grain is a texture of its own."""
    import dataclasses
    import json
    import subprocess
    import sys

    seg = dataclasses.replace(_segment(tmp_path, frames=150), theme=theme)
    spec = tmp_path / "seg.json"
    spec.write_text(draw.to_json(seg))
    subprocess.run(
        [sys.executable, "-m", "opennotebook.build.whiteboard.draw", str(spec)], check=True
    )
    parts = json.loads((tmp_path / "seg.parts.json").read_text())
    assert len(parts) >= 2, "a long hold is a piece of its own"
    listing = tmp_path / "all.txt"
    listing.write_text("".join(f"file '{p}'\n" for p in parts))
    joined = tmp_path / "joined.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(listing),
         "-c", "copy", str(joined)],
        check=True,
    )  # fmt: skip
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v",
         "-show_entries", "stream=nb_read_frames,width,height", "-of", "csv=p=0", str(joined)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()  # fmt: skip
    assert out == "1920,1080,150"


# ── the render, through the API ──────────────────────────────────────────────


# The opening and closing slides: scenes no model writes or checks.
FRAME = 2


async def _ready(client: AsyncClient) -> str:
    cid = (await client.post("/api/collections", json={"title": "Linux"})).json()["id"]
    await add_note(client, cid, "The kernel manages memory, disks and every program. " * 10)
    sid = (
        await client.post(
            f"/api/collections/{cid}/outputs", json={"kind": "slides", "slide_count": 3}
        )
    ).json()["id"]  # fmt: skip
    await drain("prep")
    assert (await client.get(f"/api/sessions/{sid}")).json()["state"] == "ready"
    return sid


async def _whiteboard(client: AsyncClient, sid: str) -> dict[str, Any]:
    r = await client.post(f"/api/sessions/{sid}/video", json={"style": "whiteboard"})
    assert r.status_code == 202, r.text
    await drain("render")
    job = (await client.get(f"/api/jobs/{r.json()['job_id']}")).json()
    states = {v["style"]: v for v in (await client.get(f"/api/sessions/{sid}/videos")).json()}
    return {"job": job, **states["whiteboard"]}


@needs_ffmpeg
async def test_a_deck_becomes_a_whiteboard_video(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    sid = await _ready(client)
    st = await _whiteboard(client, sid)
    assert st["state"] == "ready", (st["failure"], st["job"]["error"])
    assert st["job"]["steps_done"] == st["job"]["steps_total"] == 7
    assert st["scenes"] >= 2 and st["first_try"] == st["scenes"] - FRAME and st["plain"] == 0
    assert st["spent_usd"] and st["spent_usd"] > 0
    assert (
        "video_plan" in studio.asked and studio.asked.count("video_scene") == st["scenes"] - FRAME
    )
    # Every word on every board was said; every claim was checked and held.
    assert st["texts"] > 0 and st["grounded"] == st["texts"]
    assert st["claims"] > 0 and st["supported"] == st["claims"] and st["unchecked"] == 0
    assert studio.asked.count("video_check") == st["scenes"] - FRAME
    r = await client.get(f"/api/sessions/{sid}/video", params={"style": "whiteboard"})
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    # The slides video is a style of its own, untouched.
    states = (await client.get(f"/api/sessions/{sid}/videos")).json()
    assert [s["state"] for s in states] == ["none", "ready"]


@needs_ffmpeg
async def test_a_wrong_scene_is_repaired_once_and_one_never_right_is_drawn_plain(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    sid = await _ready(client)
    studio.scene_wrong = 1
    st = await _whiteboard(client, sid)
    assert st["state"] == "ready" and st["first_try"] == 0 and st["plain"] == 0
    studio.scene_wrong, studio.plan_ok = 99, False
    studio._scene_tries.clear()  # pyright: ignore[reportPrivateUsage]
    st = await _whiteboard(client, sid)
    assert st["state"] == "ready", st["failure"]
    assert st["plain"] == st["scenes"] - FRAME, "every scene drawn plain, and the video still made"


async def test_a_whiteboard_needs_an_ai_key(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    sid = await _ready(client)
    from opennotebook.ai import client as ai_client
    from opennotebook.ai.client import Ai

    monkeypatch.setattr(ai_client, "ai", lambda: Ai("http://ai.test/v1", ""))
    r = await client.post(f"/api/sessions/{sid}/video", json={"style": "whiteboard"})
    assert r.status_code == 503 and "AI key" in r.json()["detail"]


def test_lint_reads_the_drawing_it_is_given() -> None:
    empty = Drawing([], {}, [])
    assert lint.problems(empty) == ["the scene draws nothing"]
    assert SAFE[0] < SAFE[2]


@needs_ffmpeg
async def test_a_scene_the_check_rejects_is_repaired_and_one_never_fixed_is_escalated(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    sid = await _ready(client)
    studio.check_fail = 1
    st = await _whiteboard(client, sid)
    # The stand-in's narration repeats, so scenes with the same words share
    # its "first check fails": at least one is repaired, and every scene
    # ends either right first time or repaired, with every claim held.
    assert st["state"] == "ready" and st["repaired"] >= 1
    assert st["first_try"] + st["repaired"] == st["scenes"] - FRAME and st["plain"] == 0
    assert st["supported"] == st["claims"]
    studio.check_fail, studio.scene_wrong = 0, 3
    studio._scene_tries.clear()  # pyright: ignore[reportPrivateUsage]
    st = await _whiteboard(client, sid)
    assert st["state"] == "ready" and st["escalated"] == st["scenes"] - FRAME and st["plain"] == 0


@needs_ffmpeg
async def test_a_checker_that_is_down_does_not_stop_the_video(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    sid = await _ready(client)
    studio.check_down = True
    st = await _whiteboard(client, sid)
    assert st["state"] == "ready" and st["unchecked"] == st["scenes"] - FRAME
    assert st["grounded"] == st["texts"], "the free checks still ran"


@needs_ffmpeg
async def test_a_video_overview_is_made_from_a_collection_in_one_go(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    cid = (await client.post("/api/collections", json={"title": "Linux"})).json()["id"]
    await add_note(client, cid, "The kernel manages memory, disks and every program. " * 10)
    r = await client.post(f"/api/collections/{cid}/videos", json={"length": "short"})
    assert r.status_code == 202, r.text
    sid = r.json()["session"]["id"]
    assert r.json()["video"]["state"] == "waiting"
    listed = (await client.get(f"/api/collections/{cid}/videos")).json()
    assert [(v["session_id"], v["style"], v["state"]) for v in listed] == [
        (sid, "whiteboard", "waiting")
    ]
    await drain("prep")
    got = (await client.get(f"/api/sessions/{sid}")).json()
    assert got["state"] == "ready" and got["parts"] == 4 and got["speakers"] == 1
    assert got["title"] == "Linux", "named for its collection"
    # The build's end started the video: it is on the render queue now.
    assert (await client.get(f"/api/sessions/{sid}/videos")).json()[1]["state"] == "rendering"
    await drain("render")
    st = (await client.get(f"/api/collections/{cid}/videos")).json()[0]
    assert st["state"] == "ready", st["failure"]
    r = await client.get(f"/api/sessions/{sid}/video", params={"style": "whiteboard"})
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"


async def test_a_video_whose_output_fails_says_so(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)

    def found(key: str, name: str) -> str:
        return name

    monkeypatch.setattr(video, "tool", found)
    cid = (await client.post("/api/collections", json={"title": "Linux"})).json()["id"]
    await add_note(client, cid, "The kernel manages memory, disks and every program. " * 10)
    studio.fail["outline"] = 500
    sid = (await client.post(f"/api/collections/{cid}/videos", json={})).json()["session"]["id"]
    await drain("prep")
    assert (await client.get(f"/api/sessions/{sid}")).json()["state"] == "failed"
    st = (await client.get(f"/api/sessions/{sid}/videos")).json()[1]
    assert st["state"] == "failed" and "could not be made" in st["failure"]


# ── the watch page: the script, and the explainer ────────────────────────────


@needs_ffmpeg
async def test_a_video_keeps_its_script_and_explains_a_moment_of_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    sid = await _ready(client)
    st = await _whiteboard(client, sid)
    assert st["state"] == "ready", st["failure"]
    r = await client.get(f"/api/sessions/{sid}/video/script", params={"style": "whiteboard"})
    assert r.status_code == 200, r.text
    script = r.json()
    assert script["duration_ms"] == st["duration_ms"] and script["theme"] == "whiteboard"
    assert len(script["chapters"]) == st["chapters"] and len(script["scenes"]) == st["scenes"]
    # Opened and closed as a video: a slide of its own at each end, with
    # what it shows (the agenda, the takeaways) in the script.
    names = [c["title"] for c in script["chapters"]]
    assert names[0] == "Introduction" and names[-1] == "Recap"
    assert script["scenes"][0]["labels"] == [c["title"] for c in script["chapters"][1:-1]]
    assert script["scenes"][-1]["labels"] == ["The kernel manages memory."]
    assert script["lines"] and all(ln["text"] for ln in script["lines"])
    # Every word is timed, in order, within its line, for the captions.
    for ln in script["lines"]:
        assert " ".join(w for w, _, _ in ln["words"]).split() == ln["text"].split()
        starts = [s for _, s, _ in ln["words"]]
        assert starts == sorted(starts) and ln["start_ms"] <= starts[0] <= ln["end_ms"]
    assert all(sc["labels"] for sc in script["scenes"])
    scene = script["scenes"][-1]
    at = (scene["start_ms"] + scene["end_ms"]) // 2

    r = await client.post(
        f"/api/sessions/{sid}/video/explain",
        json={"style": "whiteboard", "t_ms": at, "mode": "explain",
              "history": [{"role": "user", "text": "What is a kernel?"}]},
    )  # fmt: skip
    assert r.status_code == 200, r.text
    got = r.json()
    # The passage cited stays, the one that is not is removed; so is the
    # moment past the video's end.
    assert got["answer"] == "The kernel manages memory [1]. It was said at [0:01], not at ."
    assert [c["n"] for c in got["citations"]] == [1] and got["citations"][0]["excerpt"]
    prompt = studio.bodies[-1]["messages"][1]["content"]
    assert scene["labels"][0] in prompt and "What is a kernel?" in prompt

    r = await client.post(
        f"/api/sessions/{sid}/video/explain", json={"style": "whiteboard", "t_ms": 0}
    )
    assert r.status_code == 422 and "question is empty" in r.json()["detail"]


def test_a_video_made_before_scripts_were_kept_is_followed_by_its_captions() -> None:
    from opennotebook.api.video import lines_of_vtt

    vtt = (
        "WEBVTT\n\n1\n00:00:00.000 --> 00:00:02.500\nThe kernel\nruns.\n\n"
        "00:01:02.000 --> 00:01:03.000\nIt schedules\n\n00:01:03.000 --> 00:01:04.000\nprograms.\n"
    )
    got = lines_of_vtt(vtt)
    assert [(ln.start_ms, ln.end_ms, ln.text) for ln in got] == [
        (0, 2500, "The kernel runs."),
        (62000, 64000, "It schedules programs."),
    ]


def test_the_explainer_is_told_what_is_on_at_the_moment() -> None:
    from opennotebook.script import explain

    script = {
        "title": "Plants",
        "duration_ms": 20_000,
        "chapters": [{"title": "Light", "start_ms": 0}, {"title": "Sugar", "start_ms": 10_000}],
        "lines": [
            {"start_ms": 0, "text": "Leaves catch light."},
            {"start_ms": 10_000, "text": "They make sugar."},
            {"start_ms": 15_000, "text": "And release oxygen."},
        ],
        "scenes": [{"title": "Making sugar", "start_ms": 9_000, "labels": ["glucose"]}],
    }
    m = explain.moment(script, 12_000)
    assert (m.chapter, m.scene, m.board) == ("Sugar", "Making sugar", ["glucose"])
    assert m.heard.endswith("They make sugar.") and m.ahead == "And release oxygen."
    assert explain.moments("At [0:05] and [0:21].", 20_000) == "At [0:05] and ."
    assert "- [0:15] And release oxygen." in explain.timeline(script)


@needs_ffmpeg
async def test_a_video_is_made_in_the_theme_asked_for(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    import asyncio
    import subprocess

    install(monkeypatch, files)
    sid = await _ready(client)
    r = await client.post(
        f"/api/sessions/{sid}/video", json={"style": "whiteboard", "theme": "chalkboard"}
    )
    assert r.status_code == 202, r.text
    assert r.json()["theme"] == "chalkboard"
    await drain("render")
    states = {v["style"]: v for v in (await client.get(f"/api/sessions/{sid}/videos")).json()}
    assert states["whiteboard"]["state"] == "ready", states["whiteboard"]["failure"]
    assert states["whiteboard"]["theme"] == "chalkboard"
    assert states["slides"]["theme"] is None
    # A frame from the middle of the video is on the slate, not white paper.
    mp4 = files / "video" / sid / "whiteboard.mp4"
    half = states["whiteboard"]["duration_ms"] / 2000
    rgb = (
        await asyncio.to_thread(
            subprocess.run,
            ["ffmpeg", "-v", "error", "-ss", f"{half:.2f}", "-i", str(mp4), "-frames:v", "1",
             "-vf", "scale=64:36", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
            capture_output=True, check=True,
        )
    ).stdout  # fmt: skip
    assert sum(rgb) / len(rgb) < 110, "the board is dark"


@needs_ffmpeg
async def test_an_illustrated_video_paints_its_scenes_and_draws_one_it_cannot(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    sid = await _ready(client)

    async def make() -> dict[str, Any]:
        r = await client.post(
            f"/api/sessions/{sid}/video", json={"style": "whiteboard", "theme": "watercolor"}
        )
        assert r.status_code == 202, r.text
        await drain("render")
        states = {v["style"]: v for v in (await client.get(f"/api/sessions/{sid}/videos")).json()}
        return states["whiteboard"]

    st = await make()
    assert st["state"] == "ready", st["failure"]
    assert st["theme"] == "watercolor"
    written = st["scenes"] - FRAME
    assert studio.asked.count("video_picture") == written
    assert studio.asked.count("video_picture_check") == written
    assert (st["illustrated"], st["fallback"]) == (written, 0)
    # Every picture made at 4 cents is on the render's bill.
    assert st["spent_usd"] >= 0.04 * written
    pictures = [b for b in studio.bodies if "image" in (b.get("modalities") or [])]
    assert all(b["image_config"] == {"aspect_ratio": "16:9"} for b in pictures)

    # Pictures with lettering are made once more, then the scene is drawn in
    # the theme's drawn twin: the video is made all the same.
    studio.picture_lettering = True
    studio.asked.clear()
    st = await make()
    assert st["state"] == "ready", st["failure"]
    assert studio.asked.count("video_picture") == 2 * written
    assert (st["illustrated"], st["fallback"]) == (0, written)
