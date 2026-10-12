"""Audio, video and YouTube sources: links and captions, recordings split and
transcribed, and the routes that add them."""

import asyncio
import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import httpx2
import pytest
from httpx import AsyncClient
from sqlalchemy import text as sql

from opennotebook import media, youtube
from opennotebook.ai import client as ai_client
from opennotebook.ai.client import Ai
from opennotebook.ai.providers import BY_KIND
from opennotebook.db.session import engine
from opennotebook.domain import sources

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


async def tone(tmp: Path, seconds: float, ext: str = "mp3", video: bool = False) -> bytes:
    """A recording made by ffmpeg: a tone, with a picture for a video."""
    out = tmp / f"t.{ext}"
    args = [
        "ffmpeg",
        "-v",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={seconds}",
    ]
    if video:
        args += ["-f", "lavfi", "-i", f"color=c=black:s=64x64:d={seconds}", "-shortest"]
    proc = await asyncio.create_subprocess_exec(*args, str(out))
    assert await proc.wait() == 0
    return out.read_bytes()


# ── YouTube ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "vid"),
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtube.com/watch?v=dQw4w9WgXcQ&t=42s", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?si=abc", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/channel/UC123", None),
        ("https://example.com/watch?v=dQw4w9WgXcQ", None),
        ("https://www.youtube.com/watch?v=short", None),
    ],
)
def test_a_youtube_link_names_its_video(url: str, vid: str | None) -> None:
    assert youtube.video_id(url) == vid


def test_captions_become_timed_paragraphs() -> None:
    lines = [(0.0, "hello there"), (30.0, "general\nkenobi"), (61.5, "a new minute"), (130.0, "")]
    assert youtube.paragraphs(lines) == ("[0:00] hello there general kenobi\n\n[1:01] a new minute")
    assert youtube.paragraphs([(3725.0, "late")]) == "[1:02:05] late"


async def test_a_video_is_read_with_its_title(monkeypatch: pytest.MonkeyPatch) -> None:
    def captions(vid: str, langs: tuple[str, ...]) -> list[tuple[float, str]]:
        return [(0.0, f"{vid} in {langs[0]}")]

    monkeypatch.setattr(youtube, "_captions", captions)

    async def titled(http: httpx.AsyncClient, url: str) -> str:
        return "A talk"

    monkeypatch.setattr(youtube, "title_of", titled)
    async with httpx.AsyncClient() as http:
        ready = await sources.prepare_youtube(http, "https://youtu.be/dQw4w9WgXcQ", ("fr", "en"))
    assert (ready.kind, ready.title, ready.url) == ("url", "A talk", "https://youtu.be/dQw4w9WgXcQ")
    assert ready.text.startswith("# A talk\n\nSource: https://youtu.be/dQw4w9WgXcQ\n\n[0:00]")
    assert "dQw4w9WgXcQ in fr" in ready.text


@pytest.mark.parametrize(
    ("error", "says"),
    [
        ("TranscriptsDisabled", "has no captions to read"),
        ("VideoUnavailable", "is not available"),
        ("RequestBlocked", "refused to answer this server"),
        ("AgeRestricted", "age-restricted"),
    ],
)
def test_a_video_without_readable_captions_says_what_to_do(
    monkeypatch: pytest.MonkeyPatch, error: str, says: str
) -> None:
    import youtube_transcript_api as yt

    exc: Any = getattr(yt, error)

    def refuse(self: Any, vid: str) -> Any:
        raise exc(vid)

    monkeypatch.setattr(yt.YouTubeTranscriptApi, "list", refuse)
    with pytest.raises(youtube.NoCaptions) as e:
        youtube._captions("dQw4w9WgXcQ", ("en",))  # pyright: ignore[reportPrivateUsage]
    assert says in str(e.value)


async def test_a_youtube_link_is_added_as_a_source(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def read(http: httpx.AsyncClient, url: str, languages: tuple[str, ...]) -> youtube.Video:
        return youtube.Video("dQw4w9WgXcQ", "A talk", "[0:00] Hello and welcome.")

    monkeypatch.setattr(youtube, "read", read)
    cid = (await client.post("/api/collections", json={"name": "Talks"})).json()["id"]
    r = await client.post(
        f"/api/collections/{cid}/sources",
        json={"kind": "urls", "urls": ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]},
    )
    assert r.status_code == 201, r.text
    [added] = r.json()
    assert added["ok"] and added["source"]["title"] == "A talk"


# ── recordings ────────────────────────────────────────────────────────────────


@needs_ffmpeg
async def test_a_recording_is_split_into_timed_pieces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media, "SEGMENT_S", 10)
    data = await tone(tmp_path, 25)
    heard: list[int] = []

    async def transcribe(audio: bytes) -> str:
        heard.append(len(audio))
        return f"piece {len(heard)}"

    text = await media.transcript("talk.mp3", data, "mp3", transcribe)
    assert len(heard) == 3
    assert [p.split("] ")[0] for p in text.split("\n\n")] == ["[0:00", "[0:10", "[0:20"]


@needs_ffmpeg
async def test_a_videos_sound_is_transcribed(tmp_path: Path) -> None:
    data = await tone(tmp_path, 2, "mp4", video=True)

    async def transcribe(audio: bytes) -> str:
        assert audio[:3] == b"ID3" or audio[:2] == b"\xff\xfb" or len(audio) > 0
        return "It hums."

    assert await media.transcript("clip.mp4", data, "mp4", transcribe) == "[0:00] It hums."


@needs_ffmpeg
async def test_a_recording_without_sound_is_refused(tmp_path: Path) -> None:
    out = tmp_path / "silent.mp4"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=1", str(out)
    )
    assert await proc.wait() == 0

    async def transcribe(audio: bytes) -> str:
        raise AssertionError("nothing to hear")

    with pytest.raises(media.Unreadable) as e:
        await media.transcript("silent.mp4", out.read_bytes(), "mp4", transcribe)
    assert str(e.value) == "silent.mp4 has no sound the studio can read."


@needs_ffmpeg
async def test_a_damaged_recording_is_refused() -> None:
    async def transcribe(audio: bytes) -> str:
        return "never"

    with pytest.raises(media.Unreadable) as e:
        await media.transcript("broken.mp3", b"not audio at all", "mp3", transcribe)
    assert "broken.mp3" in str(e.value)


# ── transcribing ──────────────────────────────────────────────────────────────


class Heard:
    def __init__(self) -> None:
        self.requests: list[httpx2.Request] = []

    def __call__(self, r: httpx2.Request) -> httpx2.Response:
        self.requests.append(r)
        if r.url.path.endswith("/audio/transcriptions"):
            return httpx2.Response(200, json={"text": " Hello from the API. "})
        return httpx2.Response(
            200,
            json={
                "model": "m",
                "choices": [
                    {"message": {"content": "Hello from a model."}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 5, "cost": 0.0001},
            },
        )

    def ai(self, kind: str) -> Ai:
        http = httpx2.AsyncClient(transport=httpx2.MockTransport(self))
        return Ai("http://ai.test/v1", "k", http=http, retries=0, preset=BY_KIND[kind])


async def test_a_provider_with_a_transcription_api_uses_it() -> None:
    heard = Heard()
    assert await heard.ai("openai").transcribe("whisper-1", b"mp3 bytes") == "Hello from the API."
    [r] = heard.requests
    assert r.url.path.endswith("/audio/transcriptions")
    assert b"whisper-1" in r.content and b"mp3 bytes" in r.content


async def test_elsewhere_a_model_that_hears_audio_writes_it_down() -> None:
    heard = Heard()
    said = await heard.ai("openrouter").transcribe("google/gemini-2.5-flash-lite", b"mp3 bytes")
    assert said == "Hello from a model."
    body = json.loads(heard.requests[0].content)
    parts = body["messages"][0]["content"]
    assert parts[1]["type"] == "input_audio" and parts[1]["input_audio"]["format"] == "mp3"


@needs_ffmpeg
async def test_an_uploaded_recording_becomes_its_transcript(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    heard = Heard()
    ai = heard.ai("openrouter")
    monkeypatch.setattr(ai_client, "ai", lambda: ai)
    cid = (await client.post("/api/collections", json={"name": "Talks"})).json()["id"]
    data = await tone(tmp_path, 2)
    r = await client.post(
        f"/api/collections/{cid}/sources/files",
        files=[("files", ("Interview.mp3", data, "audio/mpeg"))],
    )
    assert r.status_code == 201, r.text
    [added] = r.json()
    assert added["ok"], added
    assert added["source"]["title"] == "Interview"
    name = added["source"]["name"]
    text = (await client.get(f"/api/collections/{cid}/sources/{name}")).json()["text"]
    assert text == "# Interview\n\n[0:00] Hello from a model."
    # The transcription was paid for, and is on the ledger.
    async with engine().begin() as c:
        rows = (
            await c.execute(sql("SELECT kind, model FROM usage_events WHERE kind = 'sources'"))
        ).all()
    assert [tuple(r) for r in rows] == [("sources", "google/gemini-2.5-flash-lite")]


async def test_a_recording_is_refused_without_a_provider(
    client: AsyncClient, tmp_path: Path
) -> None:
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    cid = (await client.post("/api/collections", json={"name": "Talks"})).json()["id"]
    data = await tone(tmp_path, 1)
    r = await client.post(
        f"/api/collections/{cid}/sources/files",
        files=[("files", ("note.mp3", data, "audio/mpeg"))],
    )
    [added] = r.json()
    assert not added["ok"]
    assert added["error"].startswith("No AI provider is connected yet.")
