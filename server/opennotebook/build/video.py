"""An output as a video: its narration over its slides, one MP4 that plays
anywhere (`docs/video-overview-spec.md`).

The Slides style is the first: each slide held on screen while it is
narrated, the episode's audio under it, a chapter per slide, and the
narration as a subtitle track that is off until the viewer turns it on
(captions burned into the picture repeat the narration, which costs
learning: the redundancy principle). An audio overview has no slides, so
each of its chapters is shown as a title card.

Everything is timed by `timeline`, from the same plan that joins the
episode WAV, so the picture changes exactly where the sound does.

The tools are outside Python: a browser draws each slide, because a slide is
an HTML document, and ffmpeg encodes. ffmpeg runs as its own process and is
never linked in: the x264 encoder it carries is GPL. H.264 in MP4 is the one
format that plays in every browser, on iOS and Android, in WhatsApp and in
PowerPoint (VP9 and AV1 do not, measured 2026-10-05).

Files: `video/<sid>/<style>.mp4` and `video/<sid>/<style>.vtt`.
"""

import asyncio
import dataclasses
import html
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from opennotebook import storage
from opennotebook.build import slides, wav
from opennotebook.build import timeline as tl
from opennotebook.build.errors import Abandoned, BuildError
from opennotebook.db.models import Session
from opennotebook.db.session import sessionmaker
from opennotebook.domain.sessions import Part, in_order
from opennotebook.script.retrieval import Scope, retrieve

log = logging.getLogger(__name__)

STYLES = ("slides", "whiteboard")
WIDTH, HEIGHT, FPS = 1920, 1080, 30

FFMPEG_KEY = "OPENNOTEBOOK_FFMPEG"
FFPROBE_KEY = "OPENNOTEBOOK_FFPROBE"
# A Playwright channel, such as `chrome` for the installed Google Chrome.
# Empty tries Playwright's own Chromium, then Chrome.
BROWSER_KEY = "OPENNOTEBOOK_BROWSER_CHANNEL"

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


def phases_of(style: str) -> tuple[str, ...]:
    return WHITEBOARD_PHASES if style == "whiteboard" else PHASES


# A video's length may differ from its timeline by a frame and the AAC
# encoder's priming; anything more is a fault.
DURATION_SLACK_MS = 250
# Mean luma below this in every sampled second is a black video. Black in the
# limited range H.264 uses is 16; the all-black render the proof of concept
# produced measured 16 throughout. A dark slide is far above it.
BLACK_LUMA = 24
# Seconds a slide may take to draw, fonts included, before it is taken as
# it is.
SLIDE_LOAD_S = 15
ENCODE_TIMEOUT_S = 1800


class ToolMissing(BuildError):
    def __init__(self, what: str, sentence: str) -> None:
        super().__init__(f"{what} is not available")
        self.sentence = sentence


class EncodeFailed(BuildError):
    sentence = (
        "The video could not be encoded. Try again; if it keeps happening, the studio's log "
        "says why."
    )


class BadVideo(BuildError):
    sentence = "The video came out wrong and was not kept. Try again."


class AudioMissing(BuildError):
    sentence = (
        "Part of this output's narration is missing from the studio's files, so it cannot be "
        "made into a video. Make the output again to record it."
    )

    def __init__(self, path: str) -> None:
        super().__init__(f"narration clip {path} is missing or unreadable")


class NotReady(BuildError):
    sentence = "This output is not finished yet. Make the video once it is ready."


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
            {"start_ms": sp.start_ms, "end_ms": sp.end_ms, "text": sp.text, "part": sp.part_ordinal}
            for sp in timing.lines
        ],
        "scenes": board,
    }


def tool(key: str, name: str) -> str:
    """The path of ffmpeg or ffprobe: the setting, or the one on PATH."""
    # `which` checks a configured path as it checks PATH: a setting naming a
    # file that is not there is as missing as no ffmpeg at all.
    found = shutil.which(os.environ.get(key, "").strip() or name)
    if not found:
        raise ToolMissing(
            name,
            f"The studio cannot make videos: {name} is not installed on its server. Install "
            f"ffmpeg, or set {key} to where it is, then try again.",
        )
    return found


# ── drawing the slides ───────────────────────────────────────────────────────


def card(title: str, chapter: str, place: str, speakers: list[str]) -> str:
    """A title card for a part that has no slide: an audio overview's
    chapter, or a slide whose file is missing. A video never fails for one
    picture."""
    esc = html.escape
    who = esc(" · ".join(speakers))
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
html,body{{margin:0;width:{WIDTH}px;height:{HEIGHT}px;background:#f7f5ef;color:#1f2937;
font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif}}
main{{position:absolute;inset:120px 160px;display:flex;flex-direction:column;
justify-content:center}}
.title{{font-size:40px;color:#6b7280;letter-spacing:.02em;margin:0 0 28px}}
h1{{font-size:96px;line-height:1.08;margin:0 0 40px;font-weight:700}}
.place{{font-size:34px;color:#2563eb;font-weight:600;margin:0}}
.who{{position:absolute;left:160px;bottom:90px;font-size:30px;color:#6b7280}}
</style></head><body><main><p class="title">{esc(title)}</p><h1>{esc(chapter)}</h1>
<p class="place">{esc(place)}</p></main><div class="who">{who}</div></body></html>"""


async def _launch(pw: Any) -> Any:
    channel = os.environ.get(BROWSER_KEY, "").strip()
    tried: list[str] = []
    for ch in [channel] if channel else ["", "chrome"]:
        try:
            return await pw.chromium.launch(channel=ch or None)
        except Exception as e:
            tried.append(f"{ch or 'chromium'}: {str(e).splitlines()[0]}")
    raise ToolMissing(
        "a browser",
        "The studio cannot draw the slides for a video: it has no browser. Run `uv run "
        f"playwright install chromium` on its server, or set {BROWSER_KEY}=chrome to use "
        "Google Chrome, then try again.",
    ) from RuntimeError("; ".join(tried))


async def draw_stills(pages: list[tuple[str, bytes]], into: Path) -> list[Path]:
    """Each page as a PNG at the video's size: an HTML document drawn by the
    browser, a PNG taken as it is."""
    from playwright.async_api import async_playwright

    out: list[Path] = []
    async with async_playwright() as pw:
        browser = await _launch(pw)
        try:
            page = await browser.new_page(viewport={"width": WIDTH, "height": HEIGHT})
            for i, (kind, body) in enumerate(pages):
                path = into / f"still-{i:03d}.png"
                if kind == "image/png":
                    path.write_bytes(body)
                else:
                    try:
                        await page.set_content(
                            body.decode("utf-8", "replace"),
                            wait_until="networkidle",
                            timeout=SLIDE_LOAD_S * 1000,
                        )
                    except Exception:
                        # Fonts from the web that never arrive: the slide is
                        # drawn with what loaded.
                        log.warning("slide %d did not settle; drawing it as it is", i)
                    await page.screenshot(path=str(path), type="png")
                out.append(path)
        finally:
            await browser.close()
    return out


def pages_of(o: Session, parts: list[Part], chapters: list[tl.Chapter]) -> list[tuple[str, bytes]]:
    """What is on screen for each chapter: its slide, or a title card."""
    by_ordinal = {p.ordinal: p for p in parts}
    speakers = [str(sp.get("display_name", "")) for sp in o.speakers if isinstance(sp, dict)]
    out: list[tuple[str, bytes]] = []
    for i, ch in enumerate(chapters):
        part = by_ordinal[ch.ordinal]
        found = None
        if o.kind == "slides":
            found = slides.render_of(
                {
                    "collection": part.collection,
                    "presentation": part.presentation,
                    "slide": part.slide,
                }
            )
        if found is not None and found[1]:
            out.append(found)
            continue
        place = f"Chapter {i + 1} of {len(chapters)}"
        out.append(("text/html", card(o.title, ch.title or o.title, place, speakers).encode()))
    return out


# ── encoding ─────────────────────────────────────────────────────────────────


def concat_list(stills: list[Path], chapters: list[tl.Chapter]) -> str:
    """ffmpeg's concat list: each still for its chapter's time. The last
    still is named twice, because the demuxer reads a file's duration from
    the entry after it."""
    lines = ["ffconcat version 1.0"]
    for path, ch in zip(stills, chapters, strict=True):
        lines.append(f"file '{path.name}'")
        lines.append(f"duration {(ch.end_ms - ch.start_ms) / 1000:.3f}")
    lines.append(f"file '{stills[-1].name}'")
    return "\n".join(lines) + "\n"


def _meta_escape(s: str) -> str:
    # One line, and no backslash at its end: ffmpeg's reader takes a line
    # ending in one as continuing onto the next, which swallowed the next
    # chapter whole.
    flat = " ".join(s.replace("\r", " ").split()).rstrip("\\").rstrip()
    return "".join("\\" + c if c in "=;#\\" else c for c in flat)


def ffmetadata(title: str, chapters: list[tl.Chapter]) -> str:
    """The title and a chapter per part, so a player can jump between them."""
    out = [";FFMETADATA1", f"title={_meta_escape(title)}"]
    for i, ch in enumerate(chapters):
        out += [
            "[CHAPTER]",
            "TIMEBASE=1/1000",
            f"START={ch.start_ms}",
            f"END={max(ch.end_ms, ch.start_ms + 1)}",
            f"title={_meta_escape(ch.title or f'Part {i + 1}')}",
        ]
    return "\n".join(out) + "\n"


def encode_args(
    ffmpeg: str,
    work: Path,
    out: Path,
    total_ms: int,
    captions: bool = True,
    drawn: bool = False,
) -> list[str]:
    """One pass: the stills at 30 fps, the episode as AAC, the captions as a
    subtitle track that is off by default, the chapters, and the index at
    the front so playback starts before the download ends."""
    # An episode with no word to caption (every line punctuation) has an
    # empty caption file, which ffmpeg refuses: it goes without the track.
    subs = ["-i", str(work / "captions.srt")] if captions else []
    meta = "3" if captions else "2"
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(work / ("segments.txt" if drawn else "stills.txt")),
        "-i", str(work / "episode.wav"),
        *subs,
        "-f", "ffmetadata", "-i", str(work / "meta.txt"),
        "-map", "0:v", "-map", "1:a", *(["-map", "2:s"] if captions else []),
        "-map_metadata", meta, "-map_chapters", meta,
        # A drawn video's scenes are encoded already, and joined as they are.
        *(["-c:v", "copy"] if drawn else [
            "-vf", (
                f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=decrease,"
                f"pad={WIDTH}:{HEIGHT}:(ow-iw)/2:(oh-ih)/2:color=white,fps={FPS},format=yuv420p"
            ),
            "-c:v", "libx264", "-preset", "veryfast", "-tune", "stillimage", "-crf", "20",
        ]),
        # Levelled to -16 LUFS, the loudness of spoken video on the web, so a
        # narration is neither faint nor shouting next to anything else played.
        "-af", "loudnorm=I=-16:TP=-1.5:LRA=11", "-ar", "48000",
        "-c:a", "aac", "-b:a", "128k",
        # Asked for off; ffmpeg's MP4 muxer still enables the first track of
        # each type, so a player may show it. The web player uses the .vtt.
        *(["-c:s", "mov_text", "-disposition:s:0", "0"] if captions else []),
        "-t", f"{total_ms / 1000:.3f}",
        "-movflags", "+faststart",
        str(out),
    ]  # fmt: skip


async def _run(args: list[str], timeout_s: float) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except BaseException:
        # Stopped or too slow: the encoder does not outlive the job.
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out, err


async def encode(
    work: Path, out: Path, total_ms: int, captions: bool = True, drawn: bool = False
) -> None:
    args = encode_args(tool(FFMPEG_KEY, "ffmpeg"), work, out, total_ms, captions, drawn)
    code, _, err = await _run(args, ENCODE_TIMEOUT_S)
    if code != 0 or not await asyncio.to_thread(out.exists):
        raise EncodeFailed(f"ffmpeg exited {code}: {err.decode(errors='replace')[-800:]}")


# ── checking ─────────────────────────────────────────────────────────────────


async def probe(path: Path) -> dict[str, Any]:
    code, out, err = await _run(
        [tool(FFPROBE_KEY, "ffprobe"), "-v", "error", "-show_format", "-show_streams",
         "-show_chapters", "-of", "json", str(path)],
        120,
    )  # fmt: skip
    if code != 0:
        raise BadVideo(f"ffprobe exited {code}: {err.decode(errors='replace')[-400:]}")
    v: Any = json.loads(out or b"{}")
    return v if isinstance(v, dict) else {}


async def brightest_luma(path: Path) -> float:
    """The brightest of the video's per-second mean lumas: below
    `BLACK_LUMA` the whole video is black."""
    code, _, err = await _run(
        [tool(FFMPEG_KEY, "ffmpeg"), "-hide_banner", "-i", str(path), "-an", "-sn",
         "-vf", "fps=1,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
         "-f", "null", "-"],
        600,
    )  # fmt: skip
    if code != 0:
        raise BadVideo(f"ffmpeg could not read the video back (exit {code})")
    values = [
        float(line.rsplit("=", 1)[1])
        for line in err.decode(errors="replace").splitlines()
        if "lavfi.signalstats.YAVG=" in line
    ]
    return max(values, default=0.0)


def problems(
    info: dict[str, Any], total_ms: int, chapters: int, captions: bool = True
) -> list[str]:
    """What is wrong with a probed video, as a list: empty when it is the
    video the timeline describes."""
    streams: list[dict[str, Any]] = [s for s in info.get("streams") or [] if isinstance(s, dict)]
    found: list[str] = []
    video = [s for s in streams if s.get("codec_type") == "video"]
    if not video or video[0].get("codec_name") != "h264":
        found.append("no H.264 video stream")
    elif (video[0].get("width"), video[0].get("height"), video[0].get("pix_fmt")) != (
        WIDTH,
        HEIGHT,
        "yuv420p",
    ):
        found.append("the video is not 1920x1080 yuv420p")
    if not any(s.get("codec_type") == "audio" and s.get("codec_name") == "aac" for s in streams):
        found.append("no AAC audio stream")
    if captions and not any(s.get("codec_type") == "subtitle" for s in streams):
        found.append("no subtitle track")
    fmt: Any = info.get("format") or {}
    try:
        seconds = float(fmt.get("duration", "0")) if isinstance(fmt, dict) else 0.0
        ms = round(seconds * 1000) if math.isfinite(seconds) else 0
    except TypeError, ValueError:
        ms = 0
    if abs(ms - total_ms) > DURATION_SLACK_MS:
        found.append(f"it lasts {ms} ms where the narration lasts {total_ms} ms")
    got = len(info.get("chapters") or [])
    if got != chapters:
        found.append(f"it has {got} chapters, not {chapters}")
    return found


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


async def gone(sid: uuid.UUID) -> bool:
    """Whether the output has been deleted."""
    async with sessionmaker()() as s:
        return await s.get(Session, sid) is None


# ── the render ───────────────────────────────────────────────────────────────

Say = Callable[[str], Awaitable[None]]
Done = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class Models:
    """Who writes a whiteboard's scenes, who checks them, and who writes a
    scene once more when repairs have not fixed it. Empty skips the step."""

    write: str = ""
    check: str = ""
    escalate: str = ""


async def render(
    sid: uuid.UUID, style: str, phase: Say, phase_done: Done, models: Models | None = None
) -> dict[str, Any]:
    """Render one output's video and keep it: returns the fields its video
    state is written with. Raises a `BuildError` saying why it could not."""
    models = models or Models()
    if style not in STYLES:
        raise BuildError(f"unknown video style {style!r}")
    async with sessionmaker()() as s:
        o = await s.get(Session, sid)
        if o is None:
            raise Abandoned(sid)
        if o.state != "ready":
            raise NotReady(f"output {sid} is {o.state}")
        parts = in_order([Part.of_json(p) for p in o.slides if isinstance(p, dict)])
    phases = phases_of(style)
    extra: dict[str, Any] = {}
    voiced: Any = None
    if style == "whiteboard" and models.write:
        # The video's own narration, written for one person to say, opened
        # and closed as a video, and voiced a paragraph at a time
        # (build/whiteboard/presenter.py).
        voiced = await _presenter(sid, o, parts, models.write, phases, phase, phase_done)
        parts, extra["spoken"] = voiced.parts, voiced.rewritten
        phases = phases[2:]
    try:
        lengths = await asyncio.to_thread(exact_lengths, parts)
        timing = tl.timeline(parts, lengths)
    except ValueError as e:
        raise NotReady(str(e)) from e
    if not timing.chapters or timing.total_ms <= 0:
        raise NotReady(f"output {sid} has no narration")
    chapters = list(timing.chapters)
    board: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="on-video-") as tmp:
        work = Path(tmp)
        await phase(phases[0])
        caps = tl.captions(timing)
        vtt = tl.vtt(caps)
        await asyncio.to_thread(
            _write_inputs, work, parts, tl.srt(caps), ffmetadata(o.title, chapters)
        )
        await phase_done(phases[0])

        out = work / "video.mp4"
        if style == "whiteboard":
            scope = Scope(o.owner_id, o.collection_id)
            extra |= await _whiteboard(
                work, models, parts, timing, phases, phase, phase_done, scope=scope,
                voiced=voiced, title=o.title,
            )  # fmt: skip
            board = extra.pop("board", [])
            await encode(work, out, timing.total_ms, captions=bool(caps), drawn=True)
        else:
            await phase(phases[1])
            stills = await draw_stills(pages_of(o, parts, chapters), work)
            (work / "stills.txt").write_text(concat_list(stills, chapters), encoding="utf-8")
            await phase_done(phases[1])
            await phase(phases[2])
            await encode(work, out, timing.total_ms, captions=bool(caps))
            await phase_done(phases[2])

        await phase(phases[-1])
        found = problems(await probe(out), timing.total_ms, len(chapters), captions=bool(caps))
        if await brightest_luma(out) < BLACK_LUMA:
            found.append("every frame is black")
        if found:
            raise BadVideo("; ".join(found))
        size = await asyncio.to_thread(lambda: out.stat().st_size)
        path = await asyncio.to_thread(storage.put_file, video_path(sid, style), out)
        await asyncio.to_thread(storage.put, captions_path(sid, style), vtt.encode())
        script = json.dumps(script_of(o.title, timing, board), ensure_ascii=False)
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


# ── the whiteboard ───────────────────────────────────────────────────────────

# Scenes drawn at once, each in a process of its own with its own encoder.
SCENE_PROCESSES = max(1, min(4, (os.cpu_count() or 2) // 2))
SCENE_TIMEOUT_S = 1200


async def _whiteboard(
    work: Path,
    models: Models,
    parts: list[Part],
    timing: tl.Timeline,
    phases: tuple[str, ...],
    phase: Say,
    phase_done: Done,
    scope: Scope | None = None,
    voiced: Any = None,
    title: str = "",
) -> dict[str, Any]:
    """Plan, write and draw the scenes, leaving `segments.txt` in `work` for
    the encoder; returns what the video state records of how it went. The
    presenter's opening and closing (`voiced`) are fixed slides of their
    own, before and after the planned scenes."""
    from opennotebook.build.whiteboard import draw, ground, write
    from opennotebook.build.whiteboard.scene import Plan, PlannedScene

    spans = {sp.line_id: sp for sp in timing.lines}
    words = {lid: [float(w.start_ms) for w in sp.words] for lid, sp in spans.items()}
    starts = {lid: float(sp.start_ms) for lid, sp in spans.items()}

    def when(b: Any) -> float:
        ws = words.get(b.line)
        return ws[min(b.word, len(ws) - 1)] if ws else starts.get(b.line, 0.0)

    model = models.write
    await phase(phases[1])
    framing = {
        o: kind
        for o, kind in ((getattr(voiced, "opening", None), "opening"),
                        (getattr(voiced, "closing", None), "closing"))
        if o is not None
    }  # fmt: skip
    middle = [p for p in parts if p.ordinal not in framing]
    inside = {ln.line_id for p in middle for ln in p.lines}
    sub = dataclasses.replace(
        timing, lines=tuple(sp for sp in timing.lines if sp.line_id in inside)
    )
    plan, planned = await write.plan(model, middle, sub)
    # The frame's slides, each a scene of its own around the planned ones.
    still: dict[int, tuple[str, Any, list[str]]] = {}
    scenes = list(plan.scenes)
    for p in parts:
        kind = framing.get(p.ordinal)
        if kind is None:
            continue
        ps = PlannedScene(lines=[ln.line_id for ln in p.lines_in_order()], layout="stack",
                          title=p.title, brief=p.title)  # fmt: skip
        scenes.insert(0 if kind == "opening" else len(scenes), ps)
    plan = Plan(scenes=scenes)
    if framing:
        slides = await _frame_slides(work, voiced, title, timing, framing)
        for i, ps in enumerate(plan.scenes):
            kind = next((k for o, k in framing.items() if _first_line(parts, o) in ps.lines), None)
            if kind is not None:
                still[i] = slides[kind]
    await phase_done(phases[1])

    # Each scene is drawn as soon as it is written: the drawing of the first
    # scenes hides behind the writing of the last.
    ffmpeg = tool(FFMPEG_KEY, "ffmpeg")
    spans_of = write.windows(plan, timing)
    n = len(plan.scenes)
    writing, drawing = asyncio.Semaphore(write.PARALLEL), asyncio.Semaphore(SCENE_PROCESSES)
    written = 0
    built: list[Any] = [None] * n
    segs: list[draw.Segment] = []
    for i, (start, end) in enumerate(spans_of):
        first, last = round(start * draw.FPS / 1000), round(end * draw.FPS / 1000)
        segs.append(
            draw.Segment(
                scene={}, words=words, line_starts=starts, start_ms=start, end_ms=end,
                first_frame=first, frames=max(last - first, 1), wipe=i + 1 < n,
                out=str(work / f"scene-{i:03d}.mp4"), ffmpeg=ffmpeg,
            )
        )  # fmt: skip

    async def one(i: int) -> None:
        nonlocal written
        ps, (start, end) = plan.scenes[i], spans_of[i]
        path = ""
        if i in still:
            # A fixed slide: nothing to write or check; what it says goes
            # into the video's script.
            path, said, _ = still[i]
            # Not counted with the written scenes' first tries or repairs.
            b = write.Built(ps, said, start, end, first_try=False, checked=True)
        else:
            async with writing:
                found = await _passages(scope, ps, parts)
                b = await write.draw_scene(
                    model, ps, parts, when, start, end,
                    passages=found, check_model=models.check, escalate_model=models.escalate,
                )  # fmt: skip
        built[i] = b
        written += 1
        if written == n:
            await phase_done(phases[2])
            await phase(phases[3])
        seg = dataclasses.replace(
            segs[i], scene=b.scene.model_dump(mode="json", by_alias=True), still=path
        )
        segs[i] = seg
        spec = work / f"scene-{i:03d}.json"
        await asyncio.to_thread(spec.write_text, draw.to_json(seg), "utf-8")
        async with drawing:
            code, _, err = await _run(
                [sys.executable, "-m", "opennotebook.build.whiteboard.draw", str(spec)],
                SCENE_TIMEOUT_S,
            )
        if code != 0:
            raise EncodeFailed(f"scene {i} failed: {err.decode(errors='replace')[-800:]}")

    await phase(phases[2])
    tasks = [asyncio.create_task(one(i)) for i in range(n)]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        # One scene failed or the render was stopped: the others stop too,
        # and their processes are killed on the way out.
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    # Each scene is one or more files (its drawing, and its long holds), in
    # the order its process listed them.
    names: list[str] = []
    for g in segs:
        listed = Path(g.out[: -len(".mp4")] + ".parts.json")
        text = await asyncio.to_thread(listed.read_text, encoding="utf-8")
        names += [Path(n).name for n in json.loads(text)]
    (work / "segments.txt").write_text(
        "ffconcat version 1.0\n" + "".join(f"file '{n}'\n" for n in names), encoding="utf-8"
    )
    await phase_done(phases[3])
    return {
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
        # Not a state field: render keeps it in the video's script.
        "board": [
            {
                "title": b.scene.title,
                "start_ms": round(b.start_ms),
                "end_ms": round(b.end_ms),
                # A slide's own words, whole: its board copy is cut to fit.
                "labels": still[i][2] if i in still else [t for _, t in ground.written(b.scene)],
                "claims": [c.text for c in b.scene.claims],
            }
            for i, b in enumerate(built)
        ],
    }


def _first_line(parts: list[Part], ordinal: int) -> str:
    p = next(p for p in parts if p.ordinal == ordinal)
    return p.lines_in_order()[0].line_id


async def _frame_slides(
    work: Path, voiced: Any, title: str, timing: tl.Timeline, framing: dict[int, str]
) -> dict[str, tuple[str, Any, list[str]]]:
    """The opening and closing slides as PNGs, each with the scene that says
    what it shows and the words it writes: drawn by the browser, or on the
    board without one."""
    from opennotebook.build.whiteboard import frame

    agenda: list[str] = list(voiced.agenda)
    starts = [c.start_ms for c in timing.chapters if c.ordinal not in framing]
    takeaways: list[str] = list(voiced.takeaways) or agenda
    minutes = round(timing.total_ms / 60_000)
    pages = {
        "opening": frame.opening_html(title, voiced.about, agenda, starts, minutes),
        "closing": frame.closing_html(title, takeaways),
    }
    says = {
        "opening": frame.board(title, agenda, ticked=False),
        "closing": frame.board(frame.CLOSING, takeaways, ticked=True),
    }
    kinds = [k for k in ("opening", "closing") if k in framing.values()]
    words = {"opening": agenda, "closing": takeaways}
    out: dict[str, tuple[str, Any, list[str]]] = {}
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
    for k, path in zip(kinds, paths, strict=True):
        out[k] = (path, says[k], words[k])
    return out


# Passages per scene: enough to check its claims against, few enough to keep
# the scene's prompt short.
PASSAGES = 4


async def _passages(scope: Scope | None, ps: Any, parts: list[Part]) -> list[str]:
    """The source passages a scene is checked against: what retrieval finds
    for what the scene is about and what its narration says, as the script
    was written from. None when there is no collection to read, or the read
    fails: the scene is then held to its narration alone."""
    if scope is None:
        return []
    by_id = {ln.line_id: ln.text for p in parts for ln in p.lines}
    query = " ".join([ps.title, ps.brief, *(by_id.get(lid, "") for lid in ps.lines)])
    try:
        g = await retrieve(scope, query[:1500], PASSAGES)
    except Exception:
        log.warning("passages for a scene could not be read", exc_info=True)
        return []
    return [*g.passages[: PASSAGES * 2], *(f"{a} ({q})" for q, a in g.qa[:PASSAGES])]


async def _presenter(
    sid: uuid.UUID,
    o: Session,
    parts: list[Part],
    model: str,
    phases: tuple[str, ...],
    phase: Say,
    phase_done: Done,
) -> Any:
    """The narration rewritten for one presenter and voiced in their voice,
    the session's first speaker's: the video's parts, its opening and
    closing among them (`presenter.Voiced`)."""
    from opennotebook import speech
    from opennotebook.build.whiteboard import presenter

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


# ── starting a render ────────────────────────────────────────────────────────

RENDER_TASK = "render_video"
# A video asked for together with the output it is made from: it starts when
# that output is ready (`after_build`).
WAITING = "waiting"


async def queue(s: Any, o: Session, style: str) -> uuid.UUID:
    """Put a render of `o` in `style` on the queue, in the caller's
    transaction, and mark the video as being made. The video made before it
    plays on while it is made, and stays if it fails."""
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
    if isinstance(keep, dict):
        videos[style]["previous"] = {k: v for k, v in keep.items() if k != "previous"}
    o.video = videos
    await jobs.defer(
        s,
        job,
        RENDER_TASK,
        {"job_id": str(job.id), "session_id": str(o.id), "style": style},
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
                if not (isinstance(v, dict) and v.get("state") == WAITING and style in STYLES):  # pyright: ignore[reportUnknownMemberType]
                    continue
                try:
                    tool(FFMPEG_KEY, "ffmpeg")
                    tool(FFPROBE_KEY, "ffprobe")
                except ToolMissing as e:
                    videos = dict(o.video or {})
                    videos[style] = {"state": "failed", "failure": e.sentence}
                    o.video = videos
                    continue
                await queue(s, o, style)
    except Exception:
        log.exception("the video asked for with output %s could not be started", sid)
