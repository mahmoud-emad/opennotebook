"""A motion video recorded and checked (`docs/motion-style.md`).

This is the whiteboard's `render.scenes` for one page instead of many
scenes: plan the narration into scenes (`whiteboard/write.plan`, whose
grouping job is the same for either style), find the passages the page's
claims may cite, write the page (`write.py`), then record it and judge
every scene — the layout lint over where the page put things (`lint.py`)
and the vision check over each scene's last frame (`check.py`) — repairing
with exactly what is wrong, up to `ROUNDS` times, the last round to the
stronger model. What is still wrong at the end falls back per scene: a
scene whose layout or timing never comes right is recorded from the plain
page (`page.fallback_page`), and a claim the checker refused one last time
is dropped from the manifest. A video is never lost to a scene, and it
asserts only what its checks held.

The recording steps the page rather than capturing it running: for each
frame one `evaluate` steps `window.__Prelude.at(t)` — the frame's exact
time on the timeline — and reads back what the page drew and where
(`STEP_JS`), then screenshots it. Frames land on their words because the
timeline is the only clock there is, and reading the marked things' boxes
every frame lets the lint see exactly when each appeared: on its word,
before it, or never.

What is left in `work`: `scene-<i>.mp4` per scene (encoded as their frames
come, as a whiteboard's segments are), `boxes.json`, and `segments.txt`
for the encoder.
"""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from playwright.async_api import Page as BrowserPage
from playwright.async_api import async_playwright

from opennotebook.build import timeline as tl
from opennotebook.build.encode import FPS, HEIGHT, WIDTH, ffmpeg, tail
from opennotebook.build.errors import BuildError
from opennotebook.build.motion import check, ground, lint, write
from opennotebook.build.motion import page as page_mod
from opennotebook.build.motion import scene as sc
from opennotebook.build.motion.write import Built
from opennotebook.build.stills import launch
from opennotebook.build.video import Models, Phase
from opennotebook.build.whiteboard import write as board
from opennotebook.build.whiteboard.scene import Plan
from opennotebook.domain.sessions import Part
from opennotebook.jobs import logs
from opennotebook.script.retrieval import Scope

log = logging.getLogger(__name__)

# A page's script may lay itself out for a moment after loading.
SETTLE_MS = 400
# One frame's step, read-back and screenshot, in seconds, before the record
# is taken to have hung.
FRAME_TIMEOUT_S = 20
# Repair rounds after the first record: the writer, then the stronger
# model, as a whiteboard's ladder ends (spec 4.7).
ROUNDS = 2
# Scene checks made at once.
CHECK_AT_ONCE = 4
# The size the vision checker sees: big enough to read a label.
SEEN = (960, 540)
# How much later than its word a thing may appear before the record calls
# it late: the word's own frame plus a moment for the reveal. A thing a
# full frame early is early too (the pen starts as the word starts).
LATE_MS = 1000 // FPS + 200
EARLY_MS = 1000 // FPS + 4

# What one evaluate does per frame: step the page to its time, then read
# its own account of itself — an error, and where it put every marked thing
# (as its box in the video's pixels: the viewport is the video's size at
# scale 1, so CSS pixels are video pixels).
STEP_JS = """(t) => {
  window.__Prelude.at(t);
  if (window.__Prelude.error) return {e: window.__Prelude.error, boxes: {}};
  const boxes = {};
  for (const el of document.querySelectorAll("[data-mn]")) {
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    const cs = getComputedStyle(el);
    if (cs.display === "none" || cs.visibility === "hidden") continue;
    boxes[el.dataset.mn] = {x: r.x, y: r.y, w: r.width, h: r.height};
  }
  return {e: null, boxes: boxes};
}"""

# The page's own account of itself as it loads: how many frame callbacks it
# registered, and the first one that threw.
STATE_JS = """() => ({
  n: window.__Prelude ? window.__Prelude.registered : 0,
  e: window.__Prelude ? window.__Prelude.error : null
})"""


class PageBroken(BuildError):
    """The page could not be recorded: what its own script said."""

    sentence = (
        "The animated page could not be recorded, so no video was made. Trying again writes a "
        "new page; if it keeps happening, the page's writer will be asked for something "
        "plainer."
    )

    def __init__(self, why: str) -> None:
        super().__init__(f"the motion page could not be recorded: {why}")


@dataclass
class Rec:
    """One record: a segment a scene, where the page put its marked things
    on every frame of it, each scene's last frame as a picture, when each
    thing first appeared, and what the script logged."""

    segments: list[str] = field(default_factory=list)
    # Scene index -> frame within it -> "kind:id" -> its box.
    seen: dict[int, dict[int, dict[str, dict[str, float]]]] = field(default_factory=dict)
    # Scene index -> "kind:id" -> the first frame (its time in ms) it was
    # on screen; nothing when it never was.
    appeared: dict[int, dict[str, int]] = field(default_factory=dict)
    frames: dict[int, bytes] = field(default_factory=dict)
    said: list[str] = field(default_factory=list)

    def boxes(self, i: int) -> list[dict[str, object]]:
        """Where scene i's things were at its last frame: what the layout
        lint measures."""
        at = self.seen.get(i, {})
        if not at:
            return []
        return [{"id": k, **v} for k, v in at[max(at)].items()]


@dataclass
class Motion:
    """What recording and checking the page left: the fields the video's
    state is written with, and what each scene shows for the video's
    script (`video.script_of`)."""

    state: dict[str, Any]
    board: list[dict[str, Any]]


def _segment_of(work: Path, i: int) -> Path:
    return work / f"scene-{i:03d}.mp4"


def segments_txt(work: Path, n: int) -> None:
    """The concat list for the encoder: every scene's segment, in order.
    Written from the scene count, so a scene re-recorded from the plain
    page simply replaces its own file."""
    (work / "segments.txt").write_text(
        "ffconcat version 1.0\n" + "".join(f"file 'scene-{i:03d}.mp4'\n" for i in range(n)),
        encoding="utf-8",
    )


def beats_of(timing: tl.Timeline, manifest_: list[dict[str, Any]]) -> dict[str, int]:
    """When each marked thing must appear: its word's start in ms, keyed by
    its `kind:id` — from the same timeline the episode's audio was joined
    on (`when`, as the whiteboard's compiler reads a beat)."""
    words: dict[str, list[int]] = {}
    starts: dict[str, int] = {}
    for sp in timing.lines:
        words[sp.line_id] = [w.start_ms for w in sp.words]
        starts[sp.line_id] = sp.start_ms

    def when(b: dict[str, Any]) -> int:
        ws = words.get(str(b.get("line", "")))
        if ws:
            return ws[min(int(b.get("word", 0)), len(ws) - 1)]
        return starts.get(str(b.get("line", "")), 0)

    out: dict[str, int] = {}
    for s in manifest_:
        for n in s.get("elements") or []:
            if str(n.get("kind", "")) in lint.BOXED:
                out[f"{n['kind']}:{n['id']}"] = when(n.get("beat") or {})
    return out


def timing_problems(appeared: dict[str, int], beats: dict[str, int]) -> list[str]:
    """What the record saw of when each thing appeared: on its word (within
    a frame and a moment), before it, or never at all."""
    found: list[str] = []
    for mn, at in beats.items():
        seen = appeared.get(mn)
        if seen is None:
            found.append(
                f'{mn} is never on screen (declare it with data-mn="{mn}" and show it from '
                "its beat)"
            )
        elif seen < at - EARLY_MS:
            found.append(f"{mn} appears {at - seen} ms before its word: draw it on the word")
        elif seen > at + LATE_MS:
            found.append(f"{mn} appears {seen - at} ms after its word: appear within 200 ms")
    return found


async def record(
    manifest_: list[dict[str, Any]],
    work: Path,
    sources: dict[int, str],
    *,
    only: set[int] | None = None,
) -> Rec:
    """The page recorded scene by scene: each scene's frames stepped on the
    timeline into its own segment (encoded as they come, as the
    whiteboard's are), each scene's last frame kept for the check, and
    where the page put its marked things on every frame — which says both
    where they are (the lint) and when they appeared (`Rec.appeared`, and
    `timing_problems` against `beats`).

    `sources` gives the document to record scene `i` from — the written
    page, or the plain one for a scene that fell back — and the browser
    reloads whenever the document changes. `only` records just those
    scenes, leaving the others' segment files as they are (how a plain
    fallback re-records only the scenes that need it).

    `PageBroken` when a document never registers a frame callback, or a
    callback throws while stepping: a blank video is never encoded."""
    out = Rec()
    want = set(range(len(manifest_))) if only is None else only
    errs: list[str] = []
    async with _browser() as brow:
        # What the page says while it loads and runs: a thrown script and
        # console errors are the only account of a page that draws nothing.
        brow.on("pageerror", lambda e: errs.append(f"the page threw: {str(e)[:240]}"))
        brow.on(
            "console",
            lambda m: (
                errs.append(f"the page logged an error: {m.text[:240]}")
                if m.type == "error"
                else None
            ),
        )
        current = ""
        for i, sc_ in enumerate(manifest_):
            if i not in want:
                continue
            doc = sources[i]
            if doc != current:
                errs.clear()
                await brow.set_content(doc, wait_until="load")
                await brow.wait_for_timeout(SETTLE_MS)
                state = await brow.evaluate(STATE_JS)
                if int(state.get("n") or 0) < 1:
                    raise PageBroken(
                        (state.get("e") or (errs[0] if errs else ""))
                        or f"scene {i + 1}'s page never registered anything to draw"
                    )
                current = doc
            first = round(int(sc_["start_ms"]) * FPS / 1000)
            last = round(int(sc_["end_ms"]) * FPS / 1000)
            count = max(last - first, 1)
            seg = _segment_of(work, i)
            proc = await asyncio.create_subprocess_exec(
                ffmpeg(), "-hide_banner", "-loglevel", "error", "-y",
                "-f", "image2pipe", "-vcodec", "png", "-r", str(FPS), "-i", "-",
                "-c:v", "libx264", "-preset", "veryfast", "-tune", "animation", "-crf", "20",
                "-pix_fmt", "yuv420p", "-r", str(FPS), str(seg),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )  # fmt: skip
            assert proc.stdin is not None
            marks: dict[int, dict[str, dict[str, float]]] = {}
            appeared: dict[str, int] = {}
            last_png = b""
            for f in range(count):
                t = (first + f) * 1000 // FPS
                step = await asyncio.wait_for(brow.evaluate(STEP_JS, t), FRAME_TIMEOUT_S)
                if step.get("e"):
                    proc.kill()
                    raise PageBroken(f"the page's script failed: {step['e']}")
                boxes = step.get("boxes") or {}
                marks[f] = boxes
                for mn in boxes:
                    appeared.setdefault(mn, t)
                png = await asyncio.wait_for(
                    brow.screenshot(
                        type="png", clip={"x": 0, "y": 0, "width": WIDTH, "height": HEIGHT}
                    ),
                    FRAME_TIMEOUT_S,
                )
                if not png:
                    proc.kill()
                    raise PageBroken(f"scene {i + 1} drew nothing at {t} ms")
                last_png = png
                proc.stdin.write(png)
                await proc.stdin.drain()
            proc.stdin.close()
            code = await proc.wait()
            if code != 0:
                err = await proc.stderr.read() if proc.stderr else b""
                raise PageBroken(f"scene {i + 1}'s segment failed: {tail(err, 300)}")
            out.segments.append(seg.name)
            out.seen[i] = marks
            out.appeared[i] = appeared
            out.frames[i] = last_png
        out.said = list(errs)
    (work / "boxes.json").write_text(
        json.dumps({str(i): out.boxes(i) for i in out.seen}), encoding="utf-8"
    )
    return out


class _browser:
    """A browser page to record into, as an async context manager."""

    def __init__(self) -> None:
        self._pw: Any = None
        self._brow: Any = None

    async def __aenter__(self) -> BrowserPage:
        self._pw = await async_playwright().start()
        try:
            self._brow = await launch(self._pw)
            return await self._brow.new_page(
                viewport={"width": WIDTH, "height": HEIGHT}, device_scale_factor=1
            )
        except BaseException:
            await self._pw.stop()
            raise

    async def __aexit__(self, *exc: object) -> None:
        await self._brow.close()
        await self._pw.stop()


async def _frame_for(png: bytes) -> bytes:
    """A scene's last frame at the size the checker reads."""
    proc = await asyncio.create_subprocess_exec(
        ffmpeg(), "-hide_banner", "-loglevel", "error", "-i", "-",
        "-vf", f"scale={SEEN[0]}:{SEEN[1]}", "-frames:v", "1",
        # A pipe has no file name to tell ffmpeg the format: PNG, as the
        # checker is sent it.
        "-f", "image2pipe", "-c:v", "png", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )  # fmt: skip
    out, err = await proc.communicate(png)
    if proc.returncode != 0:
        log.info("the checker's frame could not be made: %s", tail(err, 200))
        return b""
    return out


async def _vision(
    model: str,
    manifest_: list[dict[str, Any]],
    frames: dict[int, bytes],
    narration: list[list[str]],
    passages: list[str],
    faults: dict[int, list[str]],
    verdicts: dict[int, check.Verdict],
) -> None:
    """Each scene's last frame judged, a few at a time: its claims against
    its own narration and the passages (faults, which gate), and what the
    frame looks like wrong (advice, which earns a repair round)."""
    gate = asyncio.Semaphore(CHECK_AT_ONCE)

    async def one(i: int) -> None:
        nodes, claims = sc.nodes_of(manifest_, i), sc.claims_in(manifest_, i)
        png = frames.get(i, b"")
        if not png or not (nodes or claims):
            return
        async with gate:
            small = await _frame_for(png)
            v = (
                await check.check(model, nodes, claims, small, narration[i], passages)
                if small
                else check.Verdict(checked=False)
            )
        verdicts[i] = v
        faults.setdefault(i, []).extend(v.problems)

    await asyncio.gather(*(one(i) for i in range(len(manifest_))))


def _docs(
    timing: tl.Timeline, title: str, manifest: list[dict[str, Any]], plain: set[int], written: str
) -> dict[int, str]:
    """Which document records which scene: the written page with the whole
    manifest spliced in, or the plain one for a scene in `plain`."""
    plain_doc = page_mod.inject(
        page_mod.fallback_page(timing, title, manifest), timing, title, manifest
    )
    written_doc = page_mod.inject(written, timing, title, manifest) if written else ""
    return {i: (plain_doc if i in plain else written_doc) for i in range(len(manifest))}


def _manifest_of(built: Built, titles: list[str], windows: list[tuple[float, float]]
                 ) -> list[dict[str, Any]]:  # fmt: skip
    """The manifest as the writer declared it: one entry a scene of the
    plan, with its span; nothing declared when there is no page."""
    if built.page is None:
        return sc.manifest(titles, windows, [], [])
    return sc.manifest(titles, windows, built.page.elements, built.page.claims)


def _manifest_for(page_: Any, manifest: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The manifest rebuilt from a repaired page: its elements and claims,
    grouped by the scene each resolves to."""
    return [
        {
            **s,
            "elements": [
                n.model_dump(mode="json", by_alias=True)
                for n in page_.elements
                if n.scene == s["i"]
            ],
            "claims": [c.model_dump(mode="json") for c in page_.claims if c.scene == s["i"]],
        }
        for s in manifest
    ]


def _advice(rec: Rec) -> list[str]:
    """What the page's console logged, as something to fix in a repair: an
    error is usually a typo in a selector. Never a fault on its own — a
    page that records right with one stray log keeps its video."""
    return [f"the page's console said {m}" for m in rec.said[:4]]


async def _rounds(
    built: Built,
    plan_: Plan,
    timing: tl.Timeline,
    manifest: list[dict[str, Any]],
    work: Path,
    title: str,
    *,
    check_model: str,
    escalate_model: str,
    narration: list[list[str]],
    passages: list[str],
    said: ground.Said,
    on_round: Callable[[int], Awaitable[None]] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Record, judge, repair — up to `ROUNDS` times, the last to the
    stronger model — then fall back per scene for what is still wrong.
    Returns the manifest as it will stand, and how the rounds went."""
    plain: set[int] = set() if built.page is not None else set(range(len(manifest)))
    written = built.page.html if built.page is not None else ""
    rounds = 0
    escalated = False
    faults: dict[int, list[str]] = {}
    verdicts: dict[int, check.Verdict] = {}
    rec: Rec | None = None
    while True:
        await logs.say(
            f"Recording {len(manifest)} scenes at {WIDTH}x{HEIGHT}"
            + (f" (round {rounds + 1})" if rounds else "")
        )
        rec = await record(manifest, work, _docs(timing, title, manifest, plain, written))
        await logs.ok("Recorded: judging layout and timing")
        beats = beats_of(timing, manifest)
        faults = {}
        for i in range(len(manifest)):
            if i in plain:
                continue
            # The plain page marks nothing, so it has no layout to measure
            # and no beats to hold it to.
            nodes = sc.nodes_of(manifest, i)
            if any(n.kind in lint.BOXED for n in nodes):
                bad = lint.layout_problems(nodes, rec.boxes(i))
                bad += timing_problems(
                    rec.appeared.get(i, {}),
                    {k: v for k, v in beats.items() if k in {f"{n.kind}:{n.id}" for n in nodes}},
                )
                if bad:
                    faults[i] = bad
        verdicts = {}
        if check_model and built.page is not None:
            await logs.say("Checking each scene's last frame against the narration and sources")
            await _vision(check_model, manifest, rec.frames, narration, passages, faults, verdicts)
            held = sum(v.supported for v in verdicts.values())
            asked = sum(v.claims for v in verdicts.values())
            await logs.say(f"{held}/{asked} claims held up")
            if rounds == 0:
                # The picture's advice is taken once, as a whiteboard's is:
                # the first record is repaired with it; a later one stands
                # on its claims alone (spec 4.7).
                for i, v in verdicts.items():
                    faults.setdefault(i, []).extend(v.advice)
        broken = {i: v for i, v in faults.items() if v}
        if not broken:
            await logs.ok("Every scene passed")
        if not broken or rounds >= ROUNDS:
            break
        for i in sorted(broken):
            await logs.warn(f"Scene {i + 1}: {broken[i][0]}")
        rounds += 1
        await logs.step(f"Repair round {rounds}: {len(broken)} scenes need a fix")
        if on_round is not None:
            await on_round(rounds)
        writer = (
            escalate_model
            if rounds == ROUNDS and escalate_model and escalate_model != built.model
            else built.model
        )
        escalated = escalated or bool(writer and writer == escalate_model)
        told = [f"scene {i + 1}: {f}" for i in sorted(broken) for f in broken[i]] + _advice(rec)
        repaired = await write.rewrite(writer, built, plan_, timing, told) if writer else None
        if repaired is None:
            break  # the repair did not read: fall back below, stop asking
        built.page, built.first_try = repaired, False
        written = repaired.html
        manifest = _manifest_for(repaired, manifest)
    assert rec is not None
    broken = {i: v for i, v in faults.items() if v}
    if broken:
        # A scene whose layout or timing never came right: recorded from
        # the plain page, whose words are the plan's own titles — and a
        # title the narration never says is dropped rather than written.
        plain |= set(broken)
        await logs.warn("Drawing plain: " + ", ".join(f"scene {i + 1}" for i in sorted(broken)))
        for i in broken:
            title_i = str(manifest[i].get("title") or "")
            manifest[i] = {
                **manifest[i],
                "title": "" if said.unsaid(title_i) else title_i,
                "elements": [],
                "claims": [],
            }
        await record(
            manifest, work, _docs(timing, title, manifest, plain, written), only=set(broken)
        )
        built.plain = True
    for i, v in verdicts.items():
        if v.unsupported and i < len(manifest):
            manifest[i]["claims"] = [
                c
                for c in manifest[i].get("claims", [])
                if str(c.get("text", "")) not in v.unsupported
            ]
    how = {
        "rounds": rounds,
        "escalated": escalated,
        "plain": sorted(plain),
        "rec": rec,
        "verdicts": verdicts,
    }
    return manifest, how


async def run(
    work: Path,
    models: Models,
    parts: list[Part],
    timing: tl.Timeline,
    phases: tuple[str, ...],
    phase: Phase,
    phase_done: Phase,
    *,
    scope: Scope,
    title: str,
    on_round: Callable[[int], Awaitable[None]] | None = None,
) -> Motion:
    """Plan the scenes, find the passages, write the page, record it and
    judge it — the repair ladder, then per-scene fallback — leaving the
    segments and `segments.txt` in `work` for the encoder."""
    await phase(phases[1])
    await logs.say(f"Grouping {sum(len(p.lines) for p in parts)} narration lines into scenes")
    plan_, planned = await board.plan(models.write, parts, timing)
    windows = write.windows_of(plan_, timing)
    titles = [sc_.title for sc_ in plan_.scenes]
    await logs.ok(f"{len(plan_.scenes)} scenes planned")
    passages = await write.passages_of(scope, plan_, parts)
    await logs.say(f"{len(passages)} source passages found for the claims to cite")
    await phase_done(phases[1])

    said = write.said_of(parts, passages)
    await phase(phases[2])
    await logs.say(f"Writing one page for the whole narration on {models.write}")
    built = await write.write_page(
        models.write, plan_, timing, windows, passages, said, escalate_model=models.escalate
    )
    if built.page is None:
        await logs.warn("No usable page came back: every scene will be drawn plain")
    elif built.first_try:
        await logs.ok("The page passed its checks as first written")
    else:
        await logs.ok(f"The page passed its checks after a repair ({built.model})")
    await phase_done(phases[2])

    await phase(phases[3])
    manifest = _manifest_of(built, titles, windows)
    narration = write.narration_of(plan_, parts)
    manifest, how = await _rounds(
        built, plan_, timing, manifest, work, title,
        check_model=models.check, escalate_model=models.escalate,
        narration=narration, passages=passages, said=said, on_round=on_round,
    )  # fmt: skip
    segments_txt(work, len(manifest))
    await phase_done(phases[3])
    return _motion(manifest, how, built, said, models, planned)


def _motion(
    manifest: list[dict[str, Any]],
    how: dict[str, Any],
    built: Built,
    said: ground.Said,
    models: Models,
    planned: bool,
) -> Motion:
    """The video's state and its script's scenes from the manifest as it
    stands and what the checks saw."""
    verdicts: dict[int, check.Verdict] = how["verdicts"]
    plain: set[int] = set(how["plain"])
    rounds = int(how["rounds"])
    texts = grounded = 0
    for s in manifest:
        labels = [str(n.get("text", "")) for n in s.get("elements") or [] if str(n.get("text", ""))]
        t, g = ground.counts(labels, said)
        texts, grounded = texts + t, grounded + g
    checked = sum(1 for v in verdicts.values() if v.checked)
    claims = sum(v.claims for v in verdicts.values())
    supported = sum(v.supported for v in verdicts.values())
    unchecked = sum(
        1
        for i, s in enumerate(manifest)
        if s.get("claims") and not (verdicts.get(i) is not None and verdicts[i].checked)
    )
    state: dict[str, Any] = {
        "scenes": len(manifest),
        "plan_used": planned,
        "first_try": int(built.first_try and rounds == 0 and not plain),
        "repaired": int(rounds > 0 and built.page is not None and not how["escalated"]),
        "plain": len(plain),
        "escalated": int(bool(how["escalated"])),
        "texts": texts,
        "grounded": grounded,
        "claims": claims,
        "supported": supported,
        # The check could not be made for a scene's claims (the checker
        # was down): as a whiteboard's own state says it.
        "unchecked": unchecked if models.check else 0,
        "checked_scenes": checked if models.check else 0,
    }
    board_ = [
        {
            "title": str(s.get("title") or ""),
            "start_ms": int(s["start_ms"]),
            "end_ms": int(s["end_ms"]),
            "labels": sc.labels_of(s),
            "claims": sc.claims_of(s),
        }
        for s in manifest
    ]
    return Motion(state, board_)


# The timeline types this module reads (`beats_of`, `_docs`).
