"""A video's encode: ffmpeg and ffprobe found, the inputs they read, the
one pass that makes the MP4, and the checks of what it made.

ffmpeg runs as its own process and is never linked in: the x264 encoder it
carries is GPL. H.264 in MP4 is the one format that plays in every browser,
on iOS and Android, in WhatsApp and in PowerPoint (VP9 and AV1 do not,
measured 2026-10-05).
"""

import asyncio
import json
import math
import os
import shutil
from pathlib import Path
from typing import Any

from opennotebook.build import timeline as tl
from opennotebook.build.errors import BuildError

WIDTH, HEIGHT, FPS = 1920, 1080, 30

FFMPEG_KEY = "OPENNOTEBOOK_FFMPEG"
FFPROBE_KEY = "OPENNOTEBOOK_FFPROBE"

# A video's length may differ from its timeline by a frame and the AAC
# encoder's priming; anything more is a fault.
DURATION_SLACK_MS = 250
# Mean luma below this in every sampled second is a black video. Black in the
# limited range H.264 uses is 16, and an all-black render measures 16
# throughout. A dark slide is far above it.
BLACK_LUMA = 24
# Seconds a run of each tool may take before it is taken to have hung: the
# encode, the probe, and the read-back for luma, which decodes every frame.
ENCODE_TIMEOUT_S = 1800
PROBE_TIMEOUT_S = 120
LUMA_TIMEOUT_S = 600
# The tail of a failing tool's error output kept in the error raised.
ERROR_TAIL = 800


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


def ffmpeg() -> str:
    return tool(FFMPEG_KEY, "ffmpeg")


def ffprobe() -> str:
    return tool(FFPROBE_KEY, "ffprobe")


def tail(err: bytes, chars: int = ERROR_TAIL) -> str:
    """The end of a tool's error output, where it says what went wrong."""
    return err.decode(errors="replace")[-chars:]


async def run(args: list[str], timeout_s: float) -> tuple[int, bytes, bytes]:
    """Run a tool to its end: its exit code, its output and its errors."""
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except BaseException:
        # Stopped or too slow: the tool does not outlive the job.
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode or 0, out, err


# ── the inputs ───────────────────────────────────────────────────────────────


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


# ── the encode ───────────────────────────────────────────────────────────────


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


async def encode(
    work: Path, out: Path, total_ms: int, captions: bool = True, drawn: bool = False
) -> None:
    """The MP4 from the inputs in `work` (`encode_args`)."""
    args = encode_args(ffmpeg(), work, out, total_ms, captions, drawn)
    code, _, err = await run(args, ENCODE_TIMEOUT_S)
    if code != 0 or not await asyncio.to_thread(out.exists):
        raise EncodeFailed(f"ffmpeg exited {code}: {tail(err)}")


# ── the checks ───────────────────────────────────────────────────────────────


async def probe(path: Path) -> dict[str, Any]:
    """ffprobe's account of a video: its format, streams and chapters."""
    code, out, err = await run(
        [ffprobe(), "-v", "error", "-show_format", "-show_streams", "-show_chapters",
         "-of", "json", str(path)],
        PROBE_TIMEOUT_S,
    )  # fmt: skip
    if code != 0:
        raise BadVideo(f"ffprobe exited {code}: {tail(err, ERROR_TAIL // 2)}")
    v: Any = json.loads(out or b"{}")
    return v if isinstance(v, dict) else {}


async def brightest_luma(path: Path) -> float:
    """The brightest of the video's per-second mean lumas: below
    `BLACK_LUMA` the whole video is black."""
    code, _, err = await run(
        [ffmpeg(), "-hide_banner", "-i", str(path), "-an", "-sn",
         "-vf", "fps=1,signalstats,metadata=print:key=lavfi.signalstats.YAVG",
         "-f", "null", "-"],
        LUMA_TIMEOUT_S,
    )  # fmt: skip
    if code != 0:
        raise BadVideo(f"ffmpeg could not read the video back (exit {code})")
    values = [
        float(line.rsplit("=", 1)[1])
        for line in err.decode(errors="replace").splitlines()
        if "lavfi.signalstats.YAVG=" in line
    ]
    return max(values, default=0.0)


def _duration_ms(info: dict[str, Any]) -> int:
    """The probed length in ms; 0 when it is missing or not a number."""
    fmt: Any = info.get("format") or {}
    try:
        seconds = float(fmt.get("duration", "0")) if isinstance(fmt, dict) else 0.0
    except TypeError, ValueError:
        return 0
    return round(seconds * 1000) if math.isfinite(seconds) else 0


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
    ms = _duration_ms(info)
    if abs(ms - total_ms) > DURATION_SLACK_MS:
        found.append(f"it lasts {ms} ms where the narration lasts {total_ms} ms")
    got = len(info.get("chapters") or [])
    if got != chapters:
        found.append(f"it has {got} chapters, not {chapters}")
    return found
