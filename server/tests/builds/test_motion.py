"""Motion videos (`docs/motion-style.md`): the page's contract, the
manifest's grounding, the layout and timing lints, the writer's validation
and prompt, the vision check's verdict, the estimate, and the render
through the API against a stand-in model.

The end-to-end render needs ffmpeg and a browser; where either is missing
it is skipped, and the rest still runs."""

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from opennotebook.ai import client as ai_client
from opennotebook.ai.client import Ai
from opennotebook.build import encode, stills, video
from opennotebook.build import timeline as tl
from opennotebook.build.motion import check, lint, page, write
from opennotebook.build.motion import scene as sc
from opennotebook.build.whiteboard.scene import Plan, PlannedScene
from opennotebook.domain import sessions_estimate as est
from opennotebook.domain.sessions import Line, Part
from tests.builds.conftest import drain
from tests.builds.fake import install, tools_found
from tests.model import add_note


@pytest.fixture
def files(tmp_path: Path) -> Path:
    return tmp_path


def _can_render() -> bool:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return False

    async def launch() -> bool:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            try:
                b = await stills.launch(pw)
            except encode.ToolMissing:
                return False
            await b.close()
            return True

    try:
        return asyncio.run(launch())
    except Exception:
        return False


needs_tools = pytest.mark.skipif(not _can_render(), reason="needs ffmpeg, ffprobe and a browser")


def _parts() -> list[Part]:
    return [
        Part(
            "intro",
            0,
            "Intro",
            [],
            [
                Line("l0", "host", 0, "The kernel manages memory carefully.", None, 2000),
                Line("l1", "expert", 1, "It schedules every process.", None, 1000),
            ],
        ),
        Part("disk", 1, "Disks", [], [Line("l2", "host", 0, "Disks store the data.", None, 1500)]),
    ]


def _timing() -> tl.Timeline:
    """The parts' timeline; the lines are voiced in name only — a unit test
    never opens the WAV (only `video.exact_lengths` does)."""
    parts = _parts()
    for p in parts:
        for line in p.lines:
            line.audio_path = f"audio/t/{line.line_id}.wav"
    return tl.timeline(parts)


def _page(**over: Any) -> sc.Page:
    v: dict[str, Any] = {
        "html": (
            '<!doctype html><html><head><meta charset="utf-8"><title>t</title></head>'
            "<body><p>hello</p><script>window.__Prelude.onFrame(function(){});</script>"
            "</body></html>"
        ),
        "elements": [
            {
                "id": "a",
                "kind": "text",
                "text": "the kernel",
                "beat": {"line": "l0", "word": 1},
                "scene": 0,
            },
            {
                "id": "b",
                "kind": "text",
                "text": "memory",
                "beat": {"line": "l0", "word": 2},
                "scene": 0,
            },
        ],
        "claims": [{"text": "the kernel manages memory", "passages": ["p1"], "scene": 0}],
    }
    v.update(over)
    return sc.Page.model_validate(v)


# ── the contract ─────────────────────────────────────────────────────────────


def test_the_prelude_is_spliced_after_head_before_the_page_runs() -> None:
    timing = _timing()
    doc = page.inject(
        "<!doctype html><html><head><style>x</style></head><body><script>readsPrelude()</script>"
        "</body></html>",
        timing,
        "Title",
        [{"i": 0, "start_ms": 0, "end_ms": 1, "elements": [], "claims": []}],
    )
    head, _, rest = doc.partition("</head>")
    assert "window.__VIDEO" in head and "window.__BEATS" in head
    assert "window.__Prelude" in head and "x</style>" in head
    # The clock is registered before the page's own script can read it.
    assert head.index("window.__Prelude") < head.index("x</style>")
    assert "readsPrelude()" in rest


def test_the_beats_are_every_word_when_it_is_said() -> None:
    timing = _timing()
    beats = json.loads(page.beats_json(timing))
    assert [b["text"] for b in beats] == [
        "The", "kernel", "manages", "memory", "carefully.", "It", "schedules", "every",
        "process.", "Disks", "store", "the", "data.",
    ]  # fmt: skip
    assert all(b["start_ms"] <= b["end_ms"] for b in beats)
    # Chapter 2 opens where the last of the first part's lines ends.
    chapters = json.loads(page.chapters_json(timing))
    assert [c["title"] for c in chapters] == ["Intro", "Disks"]
    assert chapters[1]["start_ms"] == timing.lines[1].end_ms
    assert chapters[1]["end_ms"] == timing.total_ms


def test_a_page_that_reaches_the_network_or_skips_the_head_is_refused() -> None:
    with pytest.raises(ValueError, match="network"):
        _page(html='<!doctype html><html><head></head><body><img src="https://x/y"></body></html>')
    with pytest.raises(ValueError, match="doctype"):
        _page(html="<html><head></head><body>hi</body></html>")
    with pytest.raises(ValueError, match="head"):
        _page(html="<!doctype html><body><script>x</script></body>")


# ── the manifest ─────────────────────────────────────────────────────────────


def test_a_manifest_entry_is_a_scene_with_its_window() -> None:
    timing = _timing()
    plan = Plan(
        scenes=[
            PlannedScene(lines=["l0", "l1"], title="Memory", brief="kernel and processes"),
            PlannedScene(lines=["l2"], title="Disks"),
        ]
    )
    windows = write.windows_of(plan, timing)
    nodes, claims = resolve_ok(_page(), plan)
    manifest = sc.manifest([s.title for s in plan.scenes], windows, nodes, claims)
    assert [s["i"] for s in manifest] == [0, 1]
    assert manifest[0]["start_ms"] == 0
    assert manifest[1]["start_ms"] == windows[1][0]
    assert [n["id"] for n in manifest[0]["elements"]] == ["a", "b"]
    assert manifest[1]["elements"] == []
    assert sc.labels_of(manifest[0]) == ["the kernel", "memory"]
    assert sc.claims_of(manifest[0]) == ["the kernel manages memory"]


def resolve_ok(page_: sc.Page, plan: Plan) -> tuple[list[sc.Node], list[sc.Claim]]:
    """The manifest resolved against a plan: line ids become scene indices
    as `write.scenes_of` maps them."""
    mapping = write.scenes_of(plan)
    return sc.resolve(page_, mapping)


def test_a_line_id_scene_resolves_and_a_missing_one_falls_back() -> None:
    plan = Plan(scenes=[PlannedScene(lines=["l0"]), PlannedScene(lines=["l2"])])
    p = _page(
        elements=[
            {
                "id": "x",
                "kind": "text",
                "text": "disks",
                "beat": {"line": "l2", "word": 0},
                "scene": "l2",
            },
            {"id": "y", "kind": "text", "text": "it all", "beat": {"line": "l0", "word": 0}},
        ],
        claims=[],
    )
    nodes, _ = resolve_ok(p, plan)
    by_id = {n.id: n.scene for n in nodes}
    assert by_id == {"x": 1, "y": 0}  # the line's scene; null -> the beat's scene


# ── the grounding ────────────────────────────────────────────────────────────


def test_every_word_a_page_writes_must_have_been_said() -> None:
    said = sc_ground()
    assert lint.page_problems(_page(), said) == []
    bad = _page(
        elements=[
            {
                "id": "a",
                "kind": "text",
                "text": "quantum blockchain",
                "beat": {"line": "l0", "word": 1},
            },
        ],
        claims=[],
    )
    found = lint.page_problems(bad, said)
    assert found and "not in the narration" in found[0]
    ungrounded = _page(claims=[{"text": "the kernel is imaginary", "passages": []}])
    found = lint.page_problems(ungrounded, said)
    assert any("is not in the narration or the sources" in f for f in found)


def sc_ground() -> Any:
    from opennotebook.build.motion import ground

    parts = _parts()
    return ground.Said.of([ln.text for p in parts for ln in p.lines] + ["a passage says disks"])


def test_the_manifest_form_is_checked_before_anything_is_recorded() -> None:
    said = sc_ground()
    plan = Plan(scenes=[PlannedScene(lines=["l0"])])
    timing = _timing()
    dup = _page(
        elements=[
            {"id": "a", "kind": "text", "text": "the kernel", "beat": {"line": "l0", "word": 0}},
            {"id": "a", "kind": "text", "text": "memory", "beat": {"line": "l0", "word": 1}},
        ]
    )
    assert any("share an id" in f for f in write.validate(dup, plan, timing, said))
    past = _page(
        elements=[
            {"id": "z", "kind": "text", "text": "the kernel", "beat": {"line": "l0", "word": 99}}
        ],
        claims=[],
    )
    found = write.validate(past, plan, timing, said)
    assert any("word 99 of l0" in f for f in found)
    join = _page(
        elements=[
            {
                "id": "a",
                "kind": "line",
                "from": "a",
                "to": "nowhere",
                "beat": {"line": "l0", "word": 0},
            }
        ],
        claims=[],
    )
    assert any(
        "ids that are not in the manifest" in f for f in write.validate(join, plan, timing, said)
    )


# ── the layout lint ──────────────────────────────────────────────────────────


def _node(id_: str, kind: str = "text") -> sc.Node:
    return sc.Node.model_validate(
        {"id": id_, "kind": kind, "text": "word", "beat": {"line": "l0", "word": 0}}
    )


def box(id_: str, x: float, y: float, w: float = 200, h: float = 80) -> dict[str, object]:
    return {"id": id_, "x": x, "y": y, "w": w, "h": h}


def test_the_lint_reads_where_the_page_put_each_label() -> None:
    declared = [_node("a"), _node("b")]
    good = [box("text:a", 100, 100), box("text:b", 100, 400)]
    assert lint.layout_problems(declared, good) == []
    # Two labels on top of each other, after the graze.
    found = lint.layout_problems(declared, [box("text:a", 100, 100), box("text:b", 110, 110)])
    assert any("on top of each other" in f for f in found)
    # A label half off the frame.
    found = lint.layout_problems(declared, [box("text:a", -10, 100), box("text:b", 100, 400)])
    assert any("off the frame" in f for f in found)
    # A declared thing that is not on screen at all.
    found = lint.layout_problems(declared, [box("text:a", 100, 100)])
    assert any("not on screen" in f and "data-mn" in f for f in found)
    # Nothing reported: the page marked nothing.
    found = lint.layout_problems(declared, [])
    assert any("reported nothing" in f for f in found)


def test_the_lint_lets_two_big_labels_share_a_scene_but_not_scattered_specks() -> None:
    # Two kinetic labels, small together: a legitimate scene.
    two = [_node("a"), _node("b")]
    ok = [box("text:a", 800, 400, 240, 60), box("text:b", 800, 600, 240, 60)]
    assert lint.layout_problems(two, ok) == []
    # Four things clustered in the middle: a small island in a big frame.
    four = [_node(c) for c in "abcd"]
    cluster = [
        box("text:a", 760, 440, 180, 60), box("text:b", 980, 440, 180, 60),
        box("text:c", 760, 640, 180, 60), box("text:d", 980, 640, 180, 60),
    ]  # fmt: skip
    assert any("fill only" in f for f in lint.layout_problems(four, cluster))


def test_a_thing_on_screen_before_its_word_or_never_is_a_timing_fault() -> None:
    beats = {"text:a": 1000, "text:b": 2000}
    assert lint_timing({"text:a": 1000, "text:b": 2000}, beats) == []
    found = lint_timing({"text:a": 400, "text:b": 2000}, beats)
    assert len(found) == 1 and "before its word" in found[0]
    found = lint_timing({"text:a": 1000}, beats)
    assert any("is never on screen" in f for f in found)


def lint_timing(appeared: dict[str, int], beats: dict[str, int]) -> list[str]:
    from opennotebook.build.motion.render import timing_problems

    return timing_problems(appeared, beats)


def test_the_beats_are_the_words_of_the_manifest() -> None:
    from opennotebook.build.motion.render import beats_of

    timing = _timing()
    manifest = [
        {
            "i": 0,
            "elements": [
                {"id": "a", "kind": "text", "beat": {"line": "l0", "word": 2}},
                {"id": "b", "kind": "panel", "beat": {"line": "l1", "word": 0}},
            ],
        }
    ]
    # Panels need no box, so they carry no beat to hold the page to.
    assert beats_of(timing, manifest) == {"text:a": timing.lines[0].words[2].start_ms}


# ── the writer's prompt and its checks ───────────────────────────────────────


def test_the_writer_numbers_the_narration_and_names_each_window() -> None:
    timing = _timing()
    plan = Plan(
        scenes=[
            PlannedScene(lines=["l0"], title="Memory", brief="the kernel's work"),
            PlannedScene(lines=["l1", "l2"], title="Disks"),
        ]
    )
    windows = write.windows_of(plan, timing)
    prompt = write.user_prompt(plan, timing, windows, ["a passage"])
    assert "## Scene 1: Memory" in prompt and "## Scene 2: Disks" in prompt
    assert "[0]The [1]kernel [2]manages" in prompt
    assert f"[{windows[1][0] / 1000:.1f}s" in prompt
    assert "p1: a passage" in prompt
    assert "The video lasts" in prompt
    assert write.MARK in write.SYSTEM and "window.__Prelude" in write.SYSTEM


def test_the_vision_verdict_splits_faults_from_advice() -> None:
    v = check.judged(
        {
            "claims": [
                {"claim": "a", "supported": True, "why": ""},
                {"claim": "b", "supported": False, "why": "the sources do not say it"},
            ],
            "missing": ["the disk"],
            "unreadable": ["memory"],
            "matches_narration": False,
            "fixes": ["enlarge it"],
        },
        asked=2,
    )
    assert (v.claims, v.supported) == (2, 1)
    assert v.unsupported == ["b"]
    assert any("not supported" in p for p in v.problems)
    assert any("not recognisable" in a for a in v.advice)
    down = check.judged({}, asked=3)
    assert down.claims == 3 and down.supported == 0


def test_the_check_counts_a_scene_relations_and_claims() -> None:
    nodes = [
        sc.Node.model_validate(
            {"id": "a", "kind": "text", "text": "leaf", "beat": {"line": "l0", "word": 0}}
        ),
        sc.Node.model_validate(
            {"id": "b", "kind": "text", "text": "oxygen", "beat": {"line": "l0", "word": 1}}
        ),
        sc.Node.model_validate(
            {"id": "c", "kind": "arrow", "from": "a", "to": "b", "beat": {"line": "l0", "word": 1}}
        ),
    ]
    claims = [sc.Claim(text="the leaf releases oxygen")]
    rel = check.relations(nodes, claims)
    assert "the leaf releases oxygen" in rel
    assert any("→" in r for r in rel)


# ── the pipeline's wiring ────────────────────────────────────────────────────


def test_motion_is_a_style_with_its_own_phases() -> None:
    assert "motion" in video.STYLES
    assert video.phases_of("motion") == video.MOTION_PHASES
    assert video.phases_of("motion")[0] == "Putting the narration together"
    assert len(video.phases_of("motion")) == 5


def test_the_estimate_prices_a_motion_page_as_model_calls() -> None:
    e = est.Render(style="motion")
    lines = est.render_lines(e, 6, "m")
    assert len(lines) == 1 and lines[0].step == "Animated page"
    assert lines[0].cost == est.MOTION_USD
    assert lines[0].free is False
    deck = est.Estimate([], (0.0, 0.0, 0.0), [], 0)
    with_render = est.with_render(deck, e, 6, "m")
    assert any("not yet measured" in a for a in with_render.assumptions)
    # Slides still cost nothing at the render, and a whiteboard is untouched.
    assert est.render_lines(est.Render(style="slides"), 6, "m")[0].free
    assert est.render_lines(est.Render(style="whiteboard"), 6, "m")[0].step == "Whiteboard scenes"


async def test_the_api_offers_motion_and_wants_a_key_for_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    tools_found(monkeypatch)
    cid = (await client.post("/api/collections", json={"title": "Linux"})).json()["id"]
    await add_note(client, cid, "The kernel manages memory. " * 10)
    sid = (
        await client.post(
            f"/api/collections/{cid}/outputs", json={"kind": "slides", "slide_count": 3}
        )
    ).json()["id"]
    await drain("prep")
    # Without a key: refused at once, as a whiteboard's scenes are.
    monkeypatch.setattr(ai_client, "ai", lambda: Ai("http://ai.test/v1", ""))
    r = await client.post(f"/api/sessions/{sid}/video", json={"style": "motion"})
    assert r.status_code == 503 and "No AI provider is connected" in r.json()["detail"]
    # Every style is listed, motion among them.
    states = await client.get(f"/api/sessions/{sid}/videos")
    assert [s["style"] for s in states.json()] == list(video.STYLES)


# ── the render, through the API ──────────────────────────────────────────────


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


async def _motion(client: AsyncClient, sid: str) -> dict[str, Any]:
    r = await client.post(f"/api/sessions/{sid}/video", json={"style": "motion"})
    assert r.status_code == 202, r.text
    await drain("render")
    job = (await client.get(f"/api/jobs/{r.json()['job_id']}")).json()
    states = {v["style"]: v for v in (await client.get(f"/api/sessions/{sid}/videos")).json()}
    return {"job": job, **states["motion"]}


@needs_tools
async def test_a_deck_becomes_a_motion_video(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    sid = await _ready(client)
    st = await _motion(client, sid)
    assert st["state"] == "ready", (st["failure"], st["job"]["error"])
    assert st["job"]["steps_done"] == st["job"]["steps_total"] == 5
    assert st["scenes"] >= 2 and st["plain"] == 0
    assert st["first_try"] == 1
    assert st["spent_usd"] and st["spent_usd"] > 0
    assert "motion_page" in studio.asked and "video_plan" in studio.asked
    # Every word on screen was said, and every claim was checked and held.
    assert st["texts"] > 0 and st["grounded"] == st["texts"]
    assert st["claims"] > 0 and st["supported"] == st["claims"] and st["unchecked"] == 0
    assert studio.asked.count("motion_check") == st["scenes"]
    r = await client.get(f"/api/sessions/{sid}/video", params={"style": "motion"})
    assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
    # The other styles are untouched.
    states = (await client.get(f"/api/sessions/{sid}/videos")).json()
    assert [s["state"] for s in states] == ["none", "none", "ready"]
    # The script keeps what each scene showed.
    script = (
        await client.get(f"/api/sessions/{sid}/video/script", params={"style": "motion"})
    ).json()
    assert script["scenes"] and all(s["labels"] for s in script["scenes"])


@needs_tools
async def test_a_page_that_comes_back_broken_falls_to_the_plain_one(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    studio.page_flat = True
    sid = await _ready(client)
    st = await _motion(client, sid)
    assert st["state"] == "ready", (st["failure"], st["job"]["error"])
    # Every scene fell back, and the video was still made and is true.
    assert st["plain"] == st["scenes"]
    assert st["texts"] == 0 and st["claims"] == 0
    r = await client.get(f"/api/sessions/{sid}/video", params={"style": "motion"})
    assert r.status_code == 200


@needs_tools
async def test_a_page_whose_things_appear_before_their_words_is_repaired(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    studio, _ = install(monkeypatch, files)
    studio.page_early = 1
    sid = await _ready(client)
    st = await _motion(client, sid)
    assert st["state"] == "ready", (st["failure"], st["job"]["error"])
    # The first answer showed everything two seconds early: a repair round
    # ran, the second answer was recorded, and nothing fell back.
    assert studio.page_tries >= 2
    assert st["repaired"] == 1 and st["plain"] == 0
    assert st["grounded"] == st["texts"] and st["supported"] == st["claims"]
