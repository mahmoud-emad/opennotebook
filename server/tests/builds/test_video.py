"""Videos of outputs (`docs/video-overview-spec.md`, phase 1): the timeline
they are timed on, word timings from the speech server, captions, chapters,
the render job, and the routes that make, follow, play and download a video.

The end-to-end renders need ffmpeg and a browser; where either is missing
they are skipped, and the rest still runs."""

import asyncio
import itertools
import json
import shutil
import subprocess
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook import storage
from opennotebook.build import timeline as tl
from opennotebook.build import video
from opennotebook.db.models import Session
from opennotebook.db.session import engine
from opennotebook.domain import shares
from opennotebook.domain.sessions import Line, Part
from opennotebook.speech import words_of
from opennotebook.speech.wav import ramp_wav
from tests.builds.conftest import drain
from tests.builds.fake import install
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
                b = await video._launch(pw)  # pyright: ignore[reportPrivateUsage]
            except video.ToolMissing:
                return False
            await b.close()
            return True

    try:
        return asyncio.run(launch())
    except Exception:
        return False


CAN_RENDER = _can_render()
needs_tools = pytest.mark.skipif(not CAN_RENDER, reason="needs ffmpeg, ffprobe and a browser")


def _voiced(files: Path, parts: list[Part]) -> None:
    """Give every line a real WAV of its stated duration on the files volume."""
    for p in parts:
        for line in p.lines:
            rel = f"audio/t/{line.line_id}.wav"
            path = files / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(ramp_wav(24_000, 1, 24 * (line.duration_ms or 0)))
            line.audio_path = rel


def _parts() -> list[Part]:
    return [
        Part(
            "intro",
            0,
            "Intro",
            [],
            [
                Line("s0l0", "host", 0, "Linux began as a hobby, in 1991.", None, 2000),
                Line("s0l1", "expert", 1, "Just a hobby.", None, 1000),
            ],
        ),
        Part(
            "kernel",
            1,
            "The kernel",
            [],
            [Line("s1l0", "expert", 0, "The kernel manages memory.", None, 1500)],
        ),
    ]


# ── the timeline ─────────────────────────────────────────────────────────────


def test_the_timeline_is_the_episode_with_its_pauses(
    files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "_root", lambda: files)
    parts = _parts()
    _voiced(files, parts)
    t = tl.timeline(parts)
    # 2000, a 220 ms turn, 1000, a 650 ms chapter break, 1500.
    assert [(sp.start_ms, sp.end_ms) for sp in t.lines] == [
        (0, 2000),
        (2220, 3220),
        (3870, 5370),
    ]
    assert t.total_ms == 5370
    # The slide changes when the last one's words end, and the last runs out.
    assert [(c.start_ms, c.end_ms, c.title) for c in t.chapters] == [
        (0, 3220, "Intro"),
        (3220, 5370, "The kernel"),
    ]
    from opennotebook.build import narrate, wav

    episode = narrate.episode(parts)
    assert abs(wav.duration_of(episode, "episode") - t.total_ms) <= 3


def test_an_unvoiced_line_is_refused_rather_than_guessed() -> None:
    parts = _parts()
    with pytest.raises(ValueError, match="not voiced"):
        tl.timeline(parts)


def test_estimated_words_fill_the_line_in_order() -> None:
    words = tl.estimate("Linux began as a hobby, in 1991.", 1000, 3000)
    assert [w.text for w in words] == ["Linux", "began", "as", "a", "hobby,", "in", "1991."]
    starts = [w.start_ms for w in words]
    assert starts == sorted(starts) and starts[0] == 1000
    assert all(1000 <= w.start_ms <= w.end_ms <= 3000 for w in words)
    # The comma's pause comes after "hobby,": the gap to the next word is
    # longer than the word-to-word gap before it.
    hobby, after = words[4], words[5]
    assert after.start_ms - hobby.end_ms > words[1].start_ms - words[0].end_ms
    assert tl.estimate("— …", 0, 100) == ()


def test_measured_cues_win_when_they_match_the_words(
    files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "_root", lambda: files)
    parts = _parts()
    parts[1].lines[0].cues = [
        {"word": "The", "start_ms": 10, "end_ms": 100},
        {"word": "kernel", "start_ms": 120, "end_ms": 500},
        {"word": "manages", "start_ms": 520, "end_ms": 900},
        {"word": "memory.", "start_ms": 930, "end_ms": 1400},
    ]
    parts[0].lines[0].cues = [{"word": "Linux", "start_ms": 0, "end_ms": 300}]  # too few
    _voiced(files, parts)
    t = tl.timeline(parts)
    last = t.lines[2]
    assert last.measured and [w.start_ms for w in last.words] == [3880, 3990, 4390, 4800]
    assert not t.lines[0].measured, "a partial set of cues is not trusted word by word"


def test_captions_are_short_and_cover_every_line(
    files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "_root", lambda: files)
    parts = _parts()
    parts[0].lines[0].text = (
        "Linux began as a hobby, in 1991, when a student posted a message online about a "
        "free operating system he was writing for fun."
    )
    _voiced(files, parts)
    t = tl.timeline(parts)
    caps = tl.captions(t)
    assert all(len(c.text) <= tl.CAPTION_CHARS for c in caps)
    assert " ".join(c.text for c in caps) == " ".join(sp.text for sp in t.lines)
    assert all(c.start_ms < c.end_ms for c in caps)
    assert caps[0].start_ms == 0 and caps[-1].end_ms == t.total_ms
    srt = tl.srt(caps)
    assert srt.startswith("1\n00:00:00,000 --> ") and "\n\n2\n" in srt
    assert tl.vtt(caps).startswith("WEBVTT\n\n00:00:00.000 --> ")


def test_a_word_ending_a_clause_does_not_become_a_caption_of_its_own(
    files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "_root", lambda: files)
    parts = _parts()
    parts[0].lines[
        0
    ].text = "Welcome to the show. Today we talk about colours, and why they matter."
    _voiced(files, parts)
    texts = [c.text for c in tl.captions(tl.timeline(parts)) if c.start_ms < 2000]
    assert "colours," not in texts and all(len(t.split()) > 1 for t in texts), texts


def test_captions_never_overlap_or_run_past_the_end_and_are_escaped(
    files: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(storage, "_root", lambda: files)
    parts = _parts()
    # Every cue at one instant: the pieces have no time of their own.
    parts[1].lines[0].text = "a < b --> c & d"
    parts[1].lines[0].cues = [
        {"word": w, "start_ms": 1500, "end_ms": 1500} for w in ["a", "b", "c", "d"]
    ]
    parts[0].lines[0].text = "x" * 100
    _voiced(files, parts)
    t = tl.timeline(parts)
    caps = tl.captions(t)
    assert all(a.end_ms <= b.start_ms for a, b in itertools.pairwise(caps))
    assert all(0 <= c.start_ms < c.end_ms <= t.total_ms for c in caps)
    assert all(len(c.text) <= tl.CAPTION_CHARS for c in caps), "a long word is cut to fit"
    vtt = tl.vtt(caps)
    assert "a &lt; b → c &amp; d" in vtt and vtt.count("-->") == len(caps)
    assert "a ‹ b → c & d" in tl.srt(caps)


def test_the_timeline_follows_the_samples_not_the_rounded_lengths() -> None:
    parts = _parts()
    for p in parts:
        for line in p.lines:
            line.audio_path = "x"
    exact = {"s0l0": 2000.9, "s0l1": 1000.9, "s1l0": 1500.9}
    t = tl.timeline(parts, exact)
    assert t.total_ms == round(2000.9 + 220 + 1000.9 + 650 + 1500.9)
    assert t.lines[2].start_ms == round(2000.9 + 220 + 1000.9 + 650)


# ── word timings from the speech server ──────────────────────────────────────


def _line(text: str, cues: list[tuple[str, int, int]], dur: int = 2000) -> Line:
    return Line(
        "l", "host", 0, text, "x", dur,
        [{"word": w, "start_ms": a, "end_ms": b} for w, a, b in cues],
    )  # fmt: skip


def test_cues_are_trusted_only_for_the_words_they_spell() -> None:
    words, share = tl.align(_line("hello world", [("zebra", 0, 300), ("yak", 300, 600)]), 0)
    assert share == 0 and [w.text for w in words] == ["hello", "world"]
    # One word the server did not time sits between its timed neighbours.
    words, share = tl.align(_line("one two three", [("one", 0, 400), ("three", 1200, 1600)]), 1000)
    assert share == pytest.approx(2 / 3)
    assert [(w.start_ms, w.end_ms) for w in (words[0], words[2])] == [(1000, 1400), (2200, 2600)]
    assert 1400 <= words[1].start_ms <= words[1].end_ms <= 2200
    # A word spoken as two ("is—let's" after `speakable`) takes both cues.
    words, share = tl.align(
        _line("It is—let's go", [("It", 0, 100), ("is", 100, 200), ("let's", 300, 500),
                                ("go", 500, 700)]),
        0,
    )  # fmt: skip
    assert share == 1 and (words[1].start_ms, words[1].end_ms) == (100, 500)


def test_numbers_that_are_not_times_are_dropped() -> None:
    got = words_of(
        [
            {"word": "a", "start_time": float("nan"), "end_time": 0.2},
            {"word": "b", "start_time": float("inf"), "end_time": float("inf")},
            {"word": "c", "start_time": True, "end_time": 0.2},
            {"word": "d", "start_time": 0.1, "end_time": 0.2},
        ]
    )
    assert [w.word for w in got] == ["d"]


def test_timestamps_are_read_in_either_spelling_and_punctuation_dropped() -> None:
    got = words_of(
        [
            {"word": "Hello", "start_time": 0.1, "end_time": 0.4},
            {"word": ",", "start_time": 0.4, "end_time": 0.45},
            {"word": "world", "start": 0.5, "end": 0.9},
            {"word": "Calvin", "start_time": None, "end_time": None},
        ]
    )
    assert [(w.word, w.start_ms, w.end_ms) for w in got] == [
        ("Hello", 100, 400),
        ("world", 500, 900),
    ]


async def _collection(client: AsyncClient) -> str:
    cid = (await client.post("/api/collections", json={"title": "Reefs"})).json()["id"]
    await add_note(client, cid, "Coral reefs are built by polyps over centuries. " * 12)
    return cid


async def _deck(client: AsyncClient, kind: str = "slides") -> str:
    cid = await _collection(client)
    body: dict[str, Any] = {"kind": kind}
    if kind == "slides":
        body["slide_count"] = 3
    else:
        body["audio_format"] = "brief"
    sid = (await client.post(f"/api/collections/{cid}/outputs", json=body)).json()["id"]
    await drain("prep")
    got = (await client.get(f"/api/sessions/{sid}")).json()
    assert got["state"] == "ready", got["failure"]
    return sid


async def test_a_captioning_server_times_every_line(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    _, voice = install(monkeypatch, files)
    voice.timestamps = True
    sid = await _deck(client)
    lines = [ln for p in (await client.get(f"/api/sessions/{sid}")).json()["slides"]
             for ln in p["lines"]]  # fmt: skip
    assert lines and all(ln["cues"] for ln in lines)
    first = lines[0]["cues"][0]
    assert set(first) == {"word", "start_ms", "end_ms"} and first["start_ms"] == 0


async def test_a_server_without_timings_is_asked_once_then_voiced_plainly(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    _, voice = install(monkeypatch, files)
    sid = await _deck(client)
    lines = [ln for p in (await client.get(f"/api/sessions/{sid}")).json()["slides"]
             for ln in p["lines"]]  # fmt: skip
    assert all(ln["cues"] == [] for ln in lines)
    assert len(voice.said) == len(lines), "each line voiced once, the plain way"


# ── the pieces of the encode ─────────────────────────────────────────────────


def test_chapters_and_titles_are_escaped_for_ffmpeg() -> None:
    meta = video.ffmetadata(
        "Reefs; a = story #1", [tl.Chapter(0, "A\\B", 0, 1000), tl.Chapter(1, "", 1000, 1000)]
    )
    assert "title=Reefs\\; a \\= story \\#1" in meta
    assert "title=A\\\\B" in meta and "title=Part 2" in meta
    assert "START=1000\nEND=1001" in meta, "a chapter never ends where it starts"


def test_a_title_that_ends_in_a_backslash_keeps_every_chapter() -> None:
    meta = video.ffmetadata(
        "Deck \\", [tl.Chapter(0, "Intro \\", 0, 1000), tl.Chapter(1, "Ch\r\nmore", 1000, 2000)]
    )
    assert meta.count("[CHAPTER]") == 2
    assert not any(line.endswith("\\") for line in meta.splitlines())
    assert "title=Ch more" in meta


def test_an_episode_with_nothing_to_caption_has_no_caption_track() -> None:
    args = video.encode_args("ffmpeg", Path("/w"), Path("/w/o.mp4"), 1000, captions=False)
    assert "/w/captions.srt" not in args and "mov_text" not in args and "2:s" not in args
    assert args[args.index("-map_metadata") + 1] == "2"


def test_the_concat_list_holds_each_still_for_its_chapter() -> None:
    stills = [Path("still-000.png"), Path("still-001.png")]
    got = video.concat_list(stills, [tl.Chapter(0, "a", 0, 3220), tl.Chapter(1, "b", 3220, 5370)])
    assert got.splitlines() == [
        "ffconcat version 1.0",
        "file 'still-000.png'",
        "duration 3.220",
        "file 'still-001.png'",
        "duration 2.150",
        "file 'still-001.png'",
    ]


def test_a_probe_that_is_not_the_timeline_is_a_problem() -> None:
    video_stream: dict[str, Any] = {
        "codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
        "pix_fmt": "yuv420p",
    }  # fmt: skip
    good: dict[str, Any] = {
        "format": {"duration": "5.400"},
        "streams": [
            video_stream,
            {"codec_type": "audio", "codec_name": "aac"},
            {"codec_type": "subtitle", "codec_name": "mov_text"},
        ],
        "chapters": [{}, {}],
    }  # fmt: skip
    assert video.problems(good, 5370, 2) == []
    bad = {**good, "format": {"duration": "9.0"}, "streams": [video_stream], "chapters": []}
    assert video.problems({"format": None, "streams": None, "chapters": None}, 0, 0)
    assert "lasts 0 ms" in " ".join(
        video.problems({**good, "format": {"duration": "inf"}}, 5370, 2)
    )
    found = " ".join(video.problems(bad, 5370, 2))
    assert "AAC" in found and "subtitle" in found and "lasts 9000 ms" in found
    assert "0 chapters" in found


# ── the routes ───────────────────────────────────────────────────────────────


async def test_only_a_finished_output_gets_a_video(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    cid = await _collection(client)
    sid = (await client.post(f"/api/collections/{cid}/outputs", json={"kind": "slides"})).json()[
        "id"
    ]
    r = await client.post(f"/api/sessions/{sid}/video", json={"style": "slides"})
    assert r.status_code == 409 and "still being made" in r.json()["detail"]
    r = await client.get(f"/api/sessions/{sid}/video")
    assert r.status_code == 404
    assert (await client.post(f"/api/sessions/{uuid.uuid4()}/video", json={})).status_code == 404


async def test_no_encoder_is_said_at_once(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    sid = await _deck(client)
    monkeypatch.setenv(video.FFMPEG_KEY, str(files / "no-ffmpeg-here"))
    r = await client.post(f"/api/sessions/{sid}/video", json={"style": "slides"})
    assert r.status_code == 503 and "ffmpeg is not installed" in r.json()["detail"]
    states = (await client.get(f"/api/sessions/{sid}/videos")).json()
    assert [(v["style"], v["state"], v["playable"]) for v in states] == [
        ("slides", "none", False),
        ("whiteboard", "none", False),
    ]


async def test_asking_twice_starts_one_render_and_a_dead_one_says_so(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)

    def found(key: str, name: str) -> str:
        return name

    monkeypatch.setattr(video, "tool", found)
    sid = await _deck(client)
    a = (await client.post(f"/api/sessions/{sid}/video", json={})).json()
    b = (await client.post(f"/api/sessions/{sid}/video", json={})).json()
    assert a["state"] == "rendering" and a["job_id"] == b["job_id"]
    async with engine().begin() as c:
        await c.execute(text("UPDATE jobs SET status = 'cancelled' WHERE id = :j"),
                        {"j": a["job_id"]})  # fmt: skip
    st = (await client.get(f"/api/sessions/{sid}/videos")).json()[0]
    assert st["state"] == "failed" and "stopped" in st["failure"]
    # The output itself stays ready.
    assert (await client.get(f"/api/sessions/{sid}")).json()["state"] == "ready"


async def test_the_last_video_plays_on_while_a_new_one_is_made_or_fails(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)

    def found(key: str, name: str) -> str:
        return name

    monkeypatch.setattr(video, "tool", found)
    sid = await _deck(client)
    mp4 = files / "video" / sid / "slides.mp4"
    mp4.parent.mkdir(parents=True)
    mp4.write_bytes(b"old video")
    ready = {"state": "ready", "path": f"video/{sid}/slides.mp4", "bytes": 9}
    async with engine().begin() as c:
        await c.execute(
            text("UPDATE sessions SET video = CAST(:v AS jsonb) WHERE id = :s"),
            {"v": json.dumps({"slides": ready}), "s": sid},
        )
    st = (await client.post(f"/api/sessions/{sid}/video", json={})).json()
    assert st["state"] == "rendering" and st["playable"]
    assert (await client.get(f"/api/sessions/{sid}/video")).content == b"old video"
    # The render's queue row is gone: it is not going, and says so; the old
    # video still plays.
    async with engine().begin() as c:
        await c.execute(text("DELETE FROM procrastinate_jobs"))
    st = (await client.get(f"/api/sessions/{sid}/videos")).json()[0]
    assert st["state"] == "failed" and st["playable"]
    assert (await client.get(f"/api/sessions/{sid}/video")).content == b"old video"


def test_a_copied_output_keeps_only_finished_videos() -> None:
    old_id, new_id, owner, cid = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    old = Session(
        id=old_id,
        owner_id=owner,
        collection_id=cid,
        kind="slides",
        title="t",
        description="",
        state="ready",
        speakers=[],
        slides=[],
        spent_usd=Decimal(0),
        spent_known=True,
        pinned=False,
        duration_ms=0,
        video={
            "slides": {"state": "ready", "path": f"video/{old_id}/slides.mp4"},
            "whiteboard": {"state": "rendering", "job_id": str(uuid.uuid4())},
        },
    )
    copy = shares.rewrite(old, new_id, owner, cid)
    assert copy.video == {"slides": {"state": "ready", "path": f"video/{new_id}/slides.mp4"}}


# ── end to end ───────────────────────────────────────────────────────────────


def _probe(path: Path) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_format", "-show_streams", "-show_chapters",
         "-of", "json", str(path)],
        capture_output=True, check=True,
    ).stdout  # fmt: skip
    return json.loads(out)


@needs_tools
async def test_a_deck_becomes_a_video_that_plays_seeks_and_is_deleted_with_it(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    sid = await _deck(client)
    r = await client.post(f"/api/sessions/{sid}/video", json={"style": "slides"})
    assert r.status_code == 202
    job_id = r.json()["job_id"]
    await drain("render")
    job = (await client.get(f"/api/jobs/{job_id}")).json()
    assert job["status"] == "done", job["error"]
    assert job["steps_done"] == job["steps_total"] == len(video.PHASES)
    st = (await client.get(f"/api/sessions/{sid}/videos")).json()[0]
    assert st["state"] == "ready" and st["chapters"] == 3 and st["bytes"] > 0

    mp4 = files / "video" / sid / "slides.mp4"
    info = _probe(mp4)
    assert len(info["chapters"]) == 3
    kinds = {s["codec_type"]: s for s in info["streams"]}
    assert kinds["video"]["codec_name"] == "h264" and kinds["audio"]["codec_name"] == "aac"
    # A caption track is there. Whether a player shows it at first is the
    # player's: ffmpeg's MP4 muxer enables the first track of every type.
    assert kinds["subtitle"]["codec_name"] == "mov_text"
    assert abs(round(float(info["format"]["duration"]) * 1000) - st["duration_ms"]) <= 250

    whole = await client.get(f"/api/sessions/{sid}/video")
    assert whole.status_code == 200 and whole.headers["content-type"] == "video/mp4"
    assert whole.content == mp4.read_bytes()
    assert whole.headers["content-disposition"].startswith("inline")
    part = await client.get(f"/api/sessions/{sid}/video", headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    dl = await client.get(f"/api/sessions/{sid}/video", params={"download": True})
    assert dl.headers["content-disposition"].startswith("attachment")
    caps = await client.get(f"/api/sessions/{sid}/video/captions")
    assert caps.status_code == 200 and caps.text.startswith("WEBVTT")

    assert (await client.delete(f"/api/sessions/{sid}")).status_code == 204
    assert not (files / "video" / sid).exists()


@needs_tools
async def test_an_audio_overview_gets_a_title_card_per_chapter(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, files: Path
) -> None:
    install(monkeypatch, files)
    sid = await _deck(client, kind="audio")
    await client.post(f"/api/sessions/{sid}/video", json={"style": "slides"})
    await drain("render")
    st = (await client.get(f"/api/sessions/{sid}/videos")).json()[0]
    assert st["state"] == "ready", st["failure"]
    chapters = len((await client.get(f"/api/sessions/{sid}")).json()["slides"])
    assert st["chapters"] == chapters
    assert len(_probe(files / "video" / sid / "slides.mp4")["chapters"]) == chapters
