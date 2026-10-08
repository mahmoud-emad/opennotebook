"""A whiteboard video's own steps, between the output and the encode: its
narration rewritten for one presenter and voiced (`presenter.py`), then its
scenes planned, written and drawn (`write.py`, `draw.py`).

Each scene is drawn as soon as it is written, in a process of its own with
its own encoder, so the drawing of the first scenes hides behind the writing
of the last. The presenter's opening and closing are fixed slides of their
own (`frame.py`), before and after the planned scenes.
"""

import asyncio
import dataclasses
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opennotebook import speech
from opennotebook.build import timeline as tl
from opennotebook.build.encode import EncodeFailed, ToolMissing, ffmpeg, run, tail
from opennotebook.build.stills import draw_stills
from opennotebook.build.video import Models, Phase
from opennotebook.build.whiteboard import draw, frame, ground, illustrate, presenter, write
from opennotebook.build.whiteboard import theme as th
from opennotebook.build.whiteboard.scene import Beat, Plan, PlannedScene, Scene
from opennotebook.db.models import Session
from opennotebook.domain.sessions import Part
from opennotebook.script.retrieval import Scope, retrieve

log = logging.getLogger(__name__)

# Scenes drawn at once, each in a process of its own with its own encoder:
# half the machine's cores, from one to four.
SCENE_PROCESSES = max(1, min(4, (os.cpu_count() or 2) // 2))
# Seconds one scene's process may take to draw and encode it.
SCENE_TIMEOUT_S = 1200
# Pictures made at once for an illustrated theme.
PICTURES_AT_ONCE = 4
# Passages per scene: enough to check its claims against, few enough to keep
# the scene's prompt short.
PASSAGES = 4
# The most characters of a scene's retrieval query.
QUERY_CHARS = 1500


@dataclass(frozen=True)
class FrameSlide:
    """An opening or closing slide: its PNG, the scene that says what it
    shows, and the words it writes, whole."""

    path: str
    scene: Scene
    words: list[str]


@dataclass(frozen=True)
class Drawn:
    """What drawing the scenes leaves besides `segments.txt`: the fields the
    video state records of how it went, and what each scene shows, which the
    video's script keeps."""

    state: dict[str, Any]
    board: list[dict[str, Any]]


# ── the narration ────────────────────────────────────────────────────────────


async def narrate(
    sid: uuid.UUID,
    o: Session,
    parts: list[Part],
    model: str,
    phases: tuple[str, ...],
    phase: Phase,
    phase_done: Phase,
) -> presenter.Voiced:
    """The narration rewritten for one presenter and voiced in their voice,
    the session's first speaker's: the video's parts, its opening and
    closing among them."""
    first = next((sp for sp in o.speakers if isinstance(sp, dict)), {})
    voice = str(first.get("voice_id", ""))
    speaker = str(first.get("speaker_id", "host"))
    await phase(phases[0])
    spoken = await presenter.rewrite(model, parts, [])
    await phase_done(phases[0])
    await phase(phases[1])
    voiced = await presenter.record(speech.speech(), sid, voice, speaker, parts, spoken)
    await phase_done(phases[1])
    return voiced


# ── the scenes ───────────────────────────────────────────────────────────────


async def scenes(
    work: Path,
    models: Models,
    parts: list[Part],
    timing: tl.Timeline,
    phases: tuple[str, ...],
    phase: Phase,
    phase_done: Phase,
    scope: Scope,
    voiced: presenter.Voiced | None,
    title: str,
) -> Drawn:
    """Plan, write and draw the scenes, leaving `segments.txt` in `work` for
    the encoder."""
    words = {sp.line_id: [float(w.start_ms) for w in sp.words] for sp in timing.lines}
    starts = {sp.line_id: float(sp.start_ms) for sp in timing.lines}

    def when(b: Beat) -> float:
        ws = words.get(b.line)
        return ws[min(b.word, len(ws) - 1)] if ws else starts.get(b.line, 0.0)

    await phase(phases[1])
    framing = _framing(voiced)
    plan, planned = await _plan(models.write, parts, timing, framing)
    still: dict[int, FrameSlide] = {}
    if voiced is not None and framing:
        slides = await _frame_slides(work, voiced, title, timing, framing)
        for i, ps in enumerate(plan.scenes):
            kind = next((k for o, k in framing.items() if _first_line(parts, o) in ps.lines), None)
            if kind is not None:
                still[i] = slides[kind]
    await phase_done(phases[1])

    encoder = ffmpeg()
    windows = write.windows(plan, timing)
    n = len(plan.scenes)
    writing, drawing = asyncio.Semaphore(write.PARALLEL), asyncio.Semaphore(SCENE_PROCESSES)
    # An illustrated theme: a picture per written scene, a few at a time.
    look = th.current()
    painting = asyncio.Semaphore(PICTURES_AT_ONCE)
    pictures = {"illustrated": 0, "fallback": 0}
    written = 0
    segs = _segments(work, windows, words, starts, encoder, look.id)

    async def one(i: int) -> write.Built:
        nonlocal written
        ps, (start, end) = plan.scenes[i], windows[i]
        slide = still.get(i)
        if slide is not None:
            # A fixed slide: nothing to write or check; what it says goes
            # into the video's script. Not counted with the written scenes'
            # first tries or repairs.
            b = write.Built(ps, slide.scene, start, end, first_try=False, checked=True)
        else:
            async with writing:
                found = await _passages(scope, ps, parts)
                b = await write.draw_scene(
                    models.write, ps, parts, when, start, end,
                    passages=found, check_model=models.check, escalate_model=models.escalate,
                )  # fmt: skip
        written += 1
        if written == n:
            await phase_done(phases[2])
            await phase(phases[3])
        seg = dataclasses.replace(
            segs[i],
            scene=b.scene.model_dump(mode="json", by_alias=True),
            still=slide.path if slide is not None else "",
        )
        if look.family == "illustrated" and slide is None:
            seg = await _illustrate(work, i, seg, look, ps, models, painting, pictures)
        segs[i] = seg
        await _draw(work / f"scene-{i:03d}.json", i, seg, drawing)
        return b

    await phase(phases[2])
    tasks = [asyncio.create_task(one(i)) for i in range(n)]
    try:
        built = await asyncio.gather(*tasks)
    except BaseException:
        # One scene failed or the render was stopped: the others stop too,
        # and their processes are killed on the way out.
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    await _join(work, segs)
    await phase_done(phases[3])
    return _drawn(built, still, planned, models, look, pictures)


def _framing(voiced: presenter.Voiced | None) -> dict[int, str]:
    """The presenter's opening and closing parts, by ordinal, with which
    each is."""
    out: dict[int, str] = {}
    if voiced is not None:
        if voiced.opening is not None:
            out[voiced.opening] = "opening"
        if voiced.closing is not None:
            out[voiced.closing] = "closing"
    return out


async def _plan(
    model: str, parts: list[Part], timing: tl.Timeline, framing: dict[int, str]
) -> tuple[Plan, bool]:
    """The model's plan for the parts between the opening and the closing,
    with a scene of its own for each of those around it; and whether the
    model's plan was used."""
    middle = [p for p in parts if p.ordinal not in framing]
    inside = {ln.line_id for p in middle for ln in p.lines}
    sub = dataclasses.replace(
        timing, lines=tuple(sp for sp in timing.lines if sp.line_id in inside)
    )
    plan, planned = await write.plan(model, middle, sub)
    scenes = list(plan.scenes)
    for p in parts:
        kind = framing.get(p.ordinal)
        if kind is None:
            continue
        ps = PlannedScene(lines=[ln.line_id for ln in p.lines_in_order()], layout="stack",
                          title=p.title, brief=p.title)  # fmt: skip
        scenes.insert(0 if kind == "opening" else len(scenes), ps)
    return Plan(scenes=scenes), planned


def _first_line(parts: list[Part], ordinal: int) -> str:
    p = next(p for p in parts if p.ordinal == ordinal)
    return p.lines_in_order()[0].line_id


async def _frame_slides(
    work: Path, voiced: presenter.Voiced, title: str, timing: tl.Timeline, framing: dict[int, str]
) -> dict[str, FrameSlide]:
    """The opening and closing slides as PNGs: drawn by the browser, or on
    the board without one."""
    agenda = list(voiced.agenda)
    starts = [c.start_ms for c in timing.chapters if c.ordinal not in framing]
    takeaways = list(voiced.takeaways) or agenda
    minutes = round(timing.total_ms / 60_000)
    pages = {
        "opening": frame.opening_html(title, voiced.about, agenda, starts, minutes),
        "closing": frame.closing_html(title, takeaways),
    }
    says = {
        "opening": frame.board(title, agenda, ticked=False),
        "closing": frame.board(frame.CLOSING, takeaways, ticked=True),
    }
    words = {"opening": agenda, "closing": takeaways}
    kinds = [k for k in ("opening", "closing") if k in framing.values()]
    try:
        drawn = await draw_stills([("text/html", pages[k].encode()) for k in kinds], work)
        paths = [str(p) for p in drawn]
    except ToolMissing:
        paths = []
        for k in kinds:
            png = work / f"frame-{k}.png"
            data = await asyncio.to_thread(frame.board_png, says[k])
            await asyncio.to_thread(png.write_bytes, data)
            paths.append(str(png))
    return {k: FrameSlide(path, says[k], words[k]) for k, path in zip(kinds, paths, strict=True)}


async def _passages(scope: Scope, ps: PlannedScene, parts: list[Part]) -> list[str]:
    """The source passages a scene is checked against: what retrieval finds
    for what the scene is about and what its narration says, as the script
    was written from. No passages when the read fails: the scene is then held to
    its narration alone."""
    by_id = {ln.line_id: ln.text for p in parts for ln in p.lines}
    query = " ".join([ps.title, ps.brief, *(by_id.get(lid, "") for lid in ps.lines)])
    try:
        g = await retrieve(scope, query[:QUERY_CHARS], PASSAGES)
    except Exception:
        log.warning("passages for a scene could not be read", exc_info=True)
        return []
    return [*g.passages[: PASSAGES * 2], *(f"{a} ({q})" for q, a in g.qa[:PASSAGES])]


# ── the drawing ──────────────────────────────────────────────────────────────


def _segments(
    work: Path,
    windows: list[tuple[float, float]],
    words: dict[str, list[float]],
    starts: dict[str, float],
    encoder: str,
    theme: str,
) -> list[draw.Segment]:
    """Each scene's segment, its scene still to be written: the frames it
    covers, and where its process writes it."""
    out: list[draw.Segment] = []
    for i, (start, end) in enumerate(windows):
        first, last = round(start * draw.FPS / 1000), round(end * draw.FPS / 1000)
        out.append(
            draw.Segment(
                scene={}, words=words, line_starts=starts, start_ms=start, end_ms=end,
                first_frame=first, frames=max(last - first, 1), wipe=i + 1 < len(windows),
                out=str(work / f"scene-{i:03d}.mp4"), ffmpeg=encoder, theme=theme,
            )
        )  # fmt: skip
    return out


async def _illustrate(
    work: Path, i: int, seg: draw.Segment, look: th.Theme, ps: PlannedScene, models: Models,
    painting: asyncio.Semaphore, counts: dict[str, int],
) -> draw.Segment:  # fmt: skip
    """A written scene's segment with its picture, or, without one, drawn in
    the theme's drawn twin: a video never fails for a picture."""
    pic = None
    if models.image:
        async with painting:
            pic = await illustrate.picture(models.image, models.check, look, ps)
    if pic is not None and pic.png is not None:
        art = work / f"art-{i:03d}.png"
        await asyncio.to_thread(art.write_bytes, pic.png)
        counts["illustrated"] += 1
        return dataclasses.replace(seg, picture=str(art))
    counts["fallback"] += 1
    return dataclasses.replace(seg, theme=look.twin)


async def _draw(spec: Path, i: int, seg: draw.Segment, drawing: asyncio.Semaphore) -> None:
    """One scene drawn and encoded by a process of its own, from its spec."""
    await asyncio.to_thread(spec.write_text, draw.to_json(seg), "utf-8")
    async with drawing:
        code, _, err = await run(
            [sys.executable, "-m", "opennotebook.build.whiteboard.draw", str(spec)],
            SCENE_TIMEOUT_S,
        )
    if code != 0:
        raise EncodeFailed(f"scene {i} failed: {tail(err)}")


async def _join(work: Path, segs: list[draw.Segment]) -> None:
    """The concat list of every scene's files, in order. Each scene is one
    or more files (its drawing, and its long holds), in the order its
    process listed them."""
    names: list[str] = []
    for seg in segs:
        listed = Path(seg.out.removesuffix(".mp4") + ".parts.json")
        text = await asyncio.to_thread(listed.read_text, encoding="utf-8")
        names += [Path(name).name for name in json.loads(text)]
    (work / "segments.txt").write_text(
        "ffconcat version 1.0\n" + "".join(f"file '{name}'\n" for name in names),
        encoding="utf-8",
    )


def _drawn(
    built: list[write.Built],
    still: dict[int, FrameSlide],
    planned: bool,
    models: Models,
    look: th.Theme,
    pictures: dict[str, int],
) -> Drawn:
    state: dict[str, Any] = {
        "scenes": len(built),
        "plan_used": planned,
        "first_try": sum(b.first_try for b in built),
        "repaired": sum(b.repaired for b in built),
        "plain": sum(b.plain for b in built),
        "escalated": sum(b.escalated for b in built),
        # What the board writes, and how much of it the narration or the
        # sources say; what the scenes claim, and how much the check found
        # supported; scenes the check could not be made for.
        "texts": sum(b.texts for b in built),
        "grounded": sum(b.grounded for b in built),
        "claims": sum(b.claims for b in built),
        "supported": sum(b.supported for b in built),
        "unchecked": sum(1 for b in built if not b.checked and not b.plain) if models.check else 0,
        # An illustrated theme's scenes shown as their picture, and those drawn
        # in its drawn twin instead.
        **(pictures if look.family == "illustrated" else {}),
    }
    board = [
        {
            "title": b.scene.title,
            "start_ms": round(b.start_ms),
            "end_ms": round(b.end_ms),
            # A slide's own words, whole: its board copy is cut to fit.
            "labels": still[i].words if i in still else [t for _, t in ground.written(b.scene)],
            "claims": [c.text for c in b.scene.claims],
        }
        for i, b in enumerate(built)
    ]
    return Drawn(state, board)
