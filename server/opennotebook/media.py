"""An audio or video file as a source: what it says, transcribed.

ffmpeg takes the sound out of the file (a video's picture is dropped), as
mono speech-quality MP3, in pieces of `SEGMENT_S` seconds. Each piece is
transcribed by the transcription model (`ai.client.Ai.transcribe`), a few at
a time, and the transcript opens each piece with its time (`[10:00]`), so a
citation says where in the recording a passage is.

ffmpeg and ffprobe are the ones videos are made with (`build/encode.py`).
"""

import asyncio
import json
import logging
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path

from opennotebook.build import encode

log = logging.getLogger(__name__)

AUDIO_EXTENSIONS = ("mp3", "m4a", "wav", "ogg", "oga", "opus", "flac", "aac")
VIDEO_EXTENSIONS = ("mp4", "m4v", "mov", "webm", "mkv")
EXTENSIONS = (*AUDIO_EXTENSIONS, *VIDEO_EXTENSIONS)

# The largest recording taken, and the longest.
MAX_BYTES = 500 * 1024 * 1024
MAX_SECONDS = 4 * 3600
# One piece sent for transcription: ten minutes of speech is about 3.5 MB
# at this quality, within what every provider takes in one request.
SEGMENT_S = 600
BITRATE = "48k"
# Pieces transcribed at once.
CONCURRENCY = 4
SPLIT_TIMEOUT_S = 900


class Unreadable(Exception):
    """Why the recording cannot be used, as a sentence."""


def is_media(ext: str) -> bool:
    return ext.lower().lstrip(".") in EXTENSIONS


def clock(seconds: float) -> str:
    s = int(seconds)
    h, m = divmod(s // 60, 60)
    return f"{h}:{m:02d}:{s % 60:02d}" if h else f"{m}:{s % 60:02d}"


async def _duration(path: Path, name: str) -> float:
    code, out, _ = await _run(
        name,
        [
            encode.ffprobe(),
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_type",
            "-of",
            "json",
            str(path),
        ],
        encode.PROBE_TIMEOUT_S,
    )
    try:
        info = json.loads(out or b"{}")
    except ValueError:
        info = {}
    streams = info.get("streams") or []
    if code != 0 or not any(s.get("codec_type") == "audio" for s in streams):
        raise Unreadable(f"{name} has no sound the studio can read.")
    try:
        return float((info.get("format") or {}).get("duration") or 0)
    except TypeError, ValueError:
        return 0.0


async def pieces(name: str, data: bytes, ext: str) -> list[tuple[float, bytes]]:
    """The recording's sound in pieces, each with the second it starts at."""
    if len(data) > MAX_BYTES:
        raise Unreadable(
            f"{name} is larger than {MAX_BYTES // (1024 * 1024)} MB. Upload a shorter part, or "
            "only its sound."
        )
    try:
        ffmpeg, _ = encode.ffmpeg(), encode.ffprobe()
    except encode.ToolMissing:
        raise Unreadable(
            "The studio cannot read audio or video: ffmpeg is not installed on its server."
        ) from None
    with tempfile.TemporaryDirectory(prefix="on-media-") as tmp:
        src = Path(tmp) / f"in.{ext}"
        await asyncio.to_thread(src.write_bytes, data)
        seconds = await _duration(src, name)
        if seconds > MAX_SECONDS:
            raise Unreadable(
                f"{name} is {clock(seconds)} long; the studio takes recordings of up to "
                f"{MAX_SECONDS // 3600} hours. Upload a shorter part."
            )
        code, _, err = await _run(
            name,
            [
                ffmpeg,
                "-v",
                "error",
                "-i",
                str(src),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-b:a",
                BITRATE,
                "-f",
                "segment",
                "-segment_time",
                str(SEGMENT_S),
                "-reset_timestamps",
                "1",
                str(Path(tmp) / "piece-%04d.mp3"),
            ],
            SPLIT_TIMEOUT_S,
        )
        if code != 0:
            log.warning("ffmpeg could not split %s: %s", name, encode.tail(err))
            raise Unreadable(
                f"{name} could not be read as audio or video: it may be damaged. Export it "
                "again, then upload it."
            )
        return await asyncio.to_thread(_read_pieces, Path(tmp))


async def _run(name: str, args: list[str], timeout_s: float) -> tuple[int, bytes, bytes]:
    """ffmpeg or ffprobe, with a run that takes too long said as a sentence."""
    try:
        return await encode.run(args, timeout_s)
    except TimeoutError:
        raise Unreadable(
            f"{name} took too long to read. Upload a shorter recording, or only its sound."
        ) from None


def _read_pieces(folder: Path) -> list[tuple[float, bytes]]:
    files = sorted(folder.glob("piece-*.mp3"))
    return [(i * float(SEGMENT_S), f.read_bytes()) for i, f in enumerate(files)]


async def transcript(
    name: str,
    data: bytes,
    ext: str,
    transcribe: Callable[[bytes], Awaitable[str]],
) -> str:
    """What the recording says, each piece opening with its time. Raises
    `Unreadable` for a file that cannot be used, and the transcription
    model's own error for a failed call."""
    parts = await pieces(name, data, ext)
    if not parts:
        raise Unreadable(f"{name} has no sound the studio can read.")
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(audio: bytes) -> str:
        async with gate:
            return await transcribe(audio)

    texts = await asyncio.gather(*(one(a) for _, a in parts))
    body = "\n\n".join(
        f"[{clock(at)}] {t.strip()}" for (at, _), t in zip(parts, texts, strict=True) if t.strip()
    )
    if not body:
        raise Unreadable(
            f"Nothing was said in {name} that could be written down. Check that it has speech."
        )
    return body
