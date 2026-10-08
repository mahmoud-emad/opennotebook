"""An output as a video: its narration over its pictures, one MP4 that plays
anywhere (`docs/video-overview-spec.md`).

Two styles. Slides: each slide held on screen while it is narrated, the
episode's audio under it, a chapter per slide (`stills.py`); an audio
overview has no slides, so each of its chapters is shown as a title card.
Whiteboard: the narration rewritten for one presenter and drawn on a board
scene by scene (`whiteboard/render.py`). Either way the narration is a
subtitle track that is off until the viewer turns it on (captions burned
into the picture repeat the narration, which costs learning: the
redundancy principle).

Everything is timed by `timeline`, from the same plan that joins the
episode WAV, so the picture changes exactly where the sound does. ffmpeg
encodes and the result is checked before it is kept (`encode.py`).

Files: `video/<sid>/<style>.mp4`, its captions `<style>.vtt` and its script
`<style>.script.json`.
"""

import asyncio
import json
import logging
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook import storage
from opennotebook.build import timeline as tl
from opennotebook.build import wav
from opennotebook.build.encode import (
    BLACK_LUMA,
    FFMPEG_KEY,
    FFPROBE_KEY,
    BadVideo,
    ToolMissing,
    brightest_luma,
    concat_list,
    encode,
    ffmetadata,
    probe,
    problems,
    tool,
)
from opennotebook.build.errors import Abandoned, BuildError
from opennotebook.db.models import Session
from opennotebook.db.session import sessionmaker
from opennotebook.domain.sessions import Part, in_order
from opennotebook.script.retrieval import Scope

log = logging.getLogger(__name__)

STYLES = ("slides", "whiteboard")

# The phases a render reports, in order, by style.
PHASES = (
    "Putting the narration together",
    "Drawing the slides",
    "Encoding the video",
    "Checking the video",
)
WHITEBOARD_PHASES = (
    "Writing the narration",
    "Recording the narration",
    "Putting the narration together",
    "Planning the scenes",
    "Writing the scenes",
    "Drawing the scenes",
    "Checking the video",
)

RENDER_TASK = "render_video"
# A video asked for together with the output it is made from: it starts when
# that output is ready (`after_build`).
WAITING = "waiting"

# Reports a phase of the render by its name: as it starts, or as it ends.
Phase = Callable[[str], Awaitable[None]]


def phases_of(style: str) -> tuple[str, ...]:
    return WHITEBOARD_PHASES if style == "whiteboard" else PHASES


class AudioMissing(BuildError):
    sentence = (
        "Part of this output's narration is missing from the studio's files, so it cannot be "
        "made into a video. Make the output again to record it."
    )

    def __init__(self, path: str) -> None:
        super().__init__(f"narration clip {path} is missing or unreadable")


class NotReady(BuildError):
    sentence = "This output is not finished yet. Make the video once it is ready."


@dataclass(frozen=True)
class Models:
    """Who writes a whiteboard's scenes, who checks them, who writes a
    scene once more when repairs have not fixed it, and who paints an
    illustrated theme's pictures (`illustrate.py`). Empty skips the step."""

    write: str = ""
    check: str = ""
    escalate: str = ""
    image: str = ""


# ── the files ────────────────────────────────────────────────────────────────


def video_dir(sid: object) -> str:
    return f"video/{sid}"


def video_path(sid: object, style: str) -> str:
    return f"{video_dir(sid)}/{style}.mp4"


def captions_path(sid: object, style: str) -> str:
    return f"{video_dir(sid)}/{style}.vtt"


def script_path(sid: object, style: str) -> str:
    return f"{video_dir(sid)}/{style}.script.json"


def script_of(title: str, timing: tl.Timeline, board: list[dict[str, Any]]) -> dict[str, Any]:
    """What the video says and shows, and when: its chapters, its narration
    line by line, and a whiteboard's scenes with what each writes on the
    board. The watch page follows the video with it, and the explainer is
    told from it what the viewer has just heard and seen."""
    return {
        "title": title,
        "duration_ms": timing.total_ms,
        "chapters": [
            {"title": c.title, "start_ms": c.start_ms, "end_ms": c.end_ms} for c in timing.chapters
        ],
        "lines": [
            {
                "start_ms": sp.start_ms,
                "end_ms": sp.end_ms,
                "text": sp.text,
                "part": sp.part_ordinal,
                # Each word as shown and when it is said: the watch page
                # lights each one up as it is spoken.
                "words": [[w.text, w.start_ms, w.end_ms] for w in sp.words],
            }
            for sp in timing.lines
        ],
        "scenes": board,
    }


# ── the inputs ───────────────────────────────────────────────────────────────


def _clip(path: str) -> bytes:
    try:
        return storage.read(path)
    except (OSError, ValueError) as e:
        raise AudioMissing(path) from e


def exact_lengths(parts: list[Part]) -> dict[str, float]:
    """Each voiced line's length in ms to the sample, read from its WAV: the
    stored `duration_ms` is rounded down, and over an hour of lines that
    drifts from the joined episode by a few hundred ms."""
    out: dict[str, float] = {}
    for line_id, path, _gap in wav.episode_plan(parts):
        pcm = wav.pcm_of(_clip(path))
        if pcm is None:
            raise AudioMissing(path)
        ch, rate, bits, data = pcm
        out[line_id] = len(data) * 1000 / (rate * ch * (bits // 8))
    return out


def _write_inputs(work: Path, parts: list[Part], srt: str, meta: str) -> None:
    clips = [(lid, _clip(path), gap) for lid, path, gap in wav.episode_plan(parts)]
    (work / "episode.wav").write_bytes(wav.join(clips))
    (work / "captions.srt").write_text(srt, encoding="utf-8")
    (work / "meta.txt").write_text(meta, encoding="utf-8")


async def _ready_output(sid: uuid.UUID) -> tuple[Session, list[Part]]:
    """The output and its parts, in order. `Abandoned` when it is gone,
    `NotReady` when it is not finished."""
    async with sessionmaker()() as s:
        o = await s.get(Session, sid)
        if o is None:
            raise Abandoned(sid)
        if o.state != "ready":
            raise NotReady(f"output {sid} is {o.state}")
        return o, in_order([Part.of_json(p) for p in o.slides if isinstance(p, dict)])


async def _timeline(sid: uuid.UUID, parts: list[Part]) -> tl.Timeline:
    """The parts' timeline, on their exact lengths. `NotReady` when a line
    is not voiced or nothing is said."""
    try:
        lengths = await asyncio.to_thread(exact_lengths, parts)
        timing = tl.timeline(parts, lengths)
    except ValueError as e:
        raise NotReady(str(e)) from e
    if not timing.chapters or timing.total_ms <= 0:
        raise NotReady(f"output {sid} has no narration")
    return timing


# ── the render ───────────────────────────────────────────────────────────────


async def render(
    sid: uuid.UUID,
    style: str,
    phase: Phase,
    phase_done: Phase,
    models: Models | None = None,
    theme: str = "whiteboard",
) -> dict[str, Any]:
    """Render one output's video and keep it: returns the fields its video
    state is written with. Raises a `BuildError` saying why it could not. A
    whiteboard is drawn in `theme` (build/whiteboard/theme.py)."""
    from opennotebook.build.whiteboard import theme as th

    models = models or Models()
    look = th.theme_of(theme)
    if style not in STYLES:
        raise BuildError(f"unknown video style {style!r}")
    o, parts = await _ready_output(sid)
    phases = phases_of(style)
    extra: dict[str, Any] = {}
    voiced = None
    if style == "whiteboard" and models.write:
        from opennotebook.build.whiteboard import render as board

        # The video's own narration, written for one person to say, opened
        # and closed as a video, and voiced a paragraph at a time.
        voiced = await board.narrate(sid, o, parts, models.write, phases, phase, phase_done)
        parts, extra["spoken"] = voiced.parts, voiced.rewritten
        phases = phases[2:]
    timing = await _timeline(sid, parts)
    chapters = list(timing.chapters)
    scenes: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="on-video-") as tmp:
        work = Path(tmp)
        await phase(phases[0])
        caps = tl.captions(timing)
        await asyncio.to_thread(
            _write_inputs, work, parts, tl.srt(caps), ffmetadata(o.title, chapters)
        )
        await phase_done(phases[0])

        out = work / "video.mp4"
        if style == "whiteboard":
            from opennotebook.build.whiteboard import render as board

            scope = Scope(o.owner_id, o.collection_id)
            # The theme is set before the scenes' tasks start, so each one,
            # and each thread it hands work to, draws and checks in it.
            with th.using(look):
                drawn = await board.scenes(
                    work, models, parts, timing, phases, phase, phase_done, scope=scope,
                    voiced=voiced, title=o.title,
                )  # fmt: skip
            extra |= drawn.state
            extra["theme"] = look.id
            scenes = drawn.board
            await encode(work, out, timing.total_ms, captions=bool(caps), drawn=True)
        else:
            from opennotebook.build import stills

            await phase(phases[1])
            pictures = await stills.draw_stills(stills.pages_of(o, parts, chapters), work)
            (work / "stills.txt").write_text(concat_list(pictures, chapters), encoding="utf-8")
            await phase_done(phases[1])
            await phase(phases[2])
            await encode(work, out, timing.total_ms, captions=bool(caps))
            await phase_done(phases[2])

        await phase(phases[-1])
        await _check(out, timing, len(chapters), captions=bool(caps))
        size = await asyncio.to_thread(lambda: out.stat().st_size)
        path = await asyncio.to_thread(storage.put_file, video_path(sid, style), out)
        await asyncio.to_thread(storage.put, captions_path(sid, style), tl.vtt(caps).encode())
        script = json.dumps(script_of(o.title, timing, scenes), ensure_ascii=False)
        await asyncio.to_thread(storage.put, script_path(sid, style), script.encode())
        await phase_done(phases[-1])
    return {
        "state": "ready",
        "path": path,
        "captions": captions_path(sid, style),
        "script": script_path(sid, style),
        "bytes": size,
        "duration_ms": timing.total_ms,
        "chapters": len(chapters),
        "measured_words": all(sp.measured for sp in timing.lines),
        "failure": None,
        "previous": None,
        "rendered_at": datetime.now(UTC).isoformat(),
        **extra,
    }


async def _check(out: Path, timing: tl.Timeline, chapters: int, captions: bool) -> None:
    """`BadVideo` unless the video is the one the timeline describes, and
    not black."""
    found = problems(await probe(out), timing.total_ms, chapters, captions=captions)
    if await brightest_luma(out) < BLACK_LUMA:
        found.append("every frame is black")
    if found:
        raise BadVideo("; ".join(found))


# ── the row ──────────────────────────────────────────────────────────────────


async def set_state(sid: uuid.UUID, style: str, **fields: Any) -> None:
    """Write one style's video state onto the output, under its lock; the
    output's own state is not touched. `Abandoned` when it is gone."""
    async with sessionmaker()() as s, s.begin():
        row = await s.scalar(
            select(Session)
            .where(Session.id == sid)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise Abandoned(sid)
        videos = dict(row.video or {})
        videos[style] = {**videos.get(style, {}), **fields}
        row.video = videos


async def gone(sid: uuid.UUID) -> bool:
    """Whether the output has been deleted."""
    async with sessionmaker()() as s:
        return await s.get(Session, sid) is None


# ── starting a render ────────────────────────────────────────────────────────


async def queue(s: AsyncSession, o: Session, style: str, theme: str = "whiteboard") -> uuid.UUID:
    """Put a render of `o` in `style` (and a whiteboard's `theme`) on the
    queue, in the caller's transaction, and mark the video as being made.
    The video made before it plays on while it is made, and stays if it
    fails."""
    from opennotebook import jobs
    from opennotebook.jobs.app import RENDER_LOCK, RENDER_QUEUE

    job = await jobs.create(
        s, o.owner_id, "video", collection_id=o.collection_id, session_id=o.id,
        steps_total=len(phases_of(style)),
    )  # fmt: skip
    videos = dict(o.video or {})
    was: dict[str, Any] = videos.get(style) or {}
    keep = was if was.get("state") == "ready" else was.get("previous")
    videos[style] = {"state": "rendering", "job_id": str(job.id), "failure": None}
    if style == "whiteboard":
        videos[style]["theme"] = theme
    if isinstance(keep, dict):
        videos[style]["previous"] = {k: v for k, v in keep.items() if k != "previous"}
    o.video = videos
    await jobs.defer(
        s,
        job,
        RENDER_TASK,
        {"job_id": str(job.id), "session_id": str(o.id), "style": style, "theme": theme},
        queue=RENDER_QUEUE,
        lock=RENDER_LOCK,
    )
    return job.id


async def after_build(sid: uuid.UUID) -> None:
    """Called when an output has just been made: start each video that was
    asked for with it. Never raises: the output is made either way, and a
    video that cannot start says why on its own state."""
    try:
        async with sessionmaker()() as s, s.begin():
            o = await s.scalar(select(Session).where(Session.id == sid).with_for_update())
            if o is None or o.state != "ready":
                return
            for style, v in dict(o.video or {}).items():
                if not (isinstance(v, dict) and v.get("state") == WAITING and style in STYLES):
                    continue
                try:
                    tool(FFMPEG_KEY, "ffmpeg")
                    tool(FFPROBE_KEY, "ffprobe")
                except ToolMissing as e:
                    videos = dict(o.video or {})
                    videos[style] = {"state": "failed", "failure": e.sentence}
                    o.video = videos
                    continue
                await queue(s, o, style, str(v.get("theme") or "whiteboard"))
    except Exception:
        log.exception("the video asked for with output %s could not be started", sid)
