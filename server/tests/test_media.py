"""The player's bytes: a line's audio, an episode, a slide. Ported from the
tests of the Rust server's byte routes (`main.rs`: the slide cache, thumbnails,
the WAV join) and `playback.rs`, plus the routes: who may read which bytes,
through an output of theirs or a share that includes it."""

import json
import struct
import uuid
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import text

from opennotebook import storage
from opennotebook.api import media
from opennotebook.config import settings
from opennotebook.db.session import engine
from tests.conftest import other_person


@pytest.fixture(autouse=True)
def files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings(), "files_dir", tmp_path)
    return tmp_path


def wav(rate: int, frames: int, *, fill: int = 0, channels: int = 1, extra: bool = False) -> bytes:
    """A 16-bit PCM WAV of `frames` frames, with a `LIST` chunk before `data`
    when `extra` is set, as some writers put one."""
    data = struct.pack("<h", fill) * frames * channels
    fmt = struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * 2 * channels, 2 * channels, 16)
    chunks = b"fmt " + fmt
    if extra:
        chunks += b"LIST" + struct.pack("<I", 5) + b"INFOx" + b"\0"
    chunks += b"data" + struct.pack("<I", len(data)) + data
    return b"RIFF" + struct.pack("<I", 4 + len(chunks)) + b"WAVE" + chunks


def wav_ms(body: bytes) -> int:
    pcm = media.pcm_of(body)
    assert pcm is not None
    channels, rate, bits, data = pcm
    return len(data) * 1000 // (rate * channels * bits // 8)


# ── the pieces ────────────────────────────────────────────────────────────────


def test_the_pause_before_a_line_follows_the_conversation() -> None:
    assert media.gap_before(True, True) == 650, "a new chapter is a breath longer"
    assert media.gap_before(True, False) == 650
    assert media.gap_before(False, True) == 320, "the same speaker going on"
    assert media.gap_before(False, False) == 220, "the other speaker answering"


def test_clips_join_into_one_wav_with_silence_between() -> None:
    # 24 kHz mono: 0.5 s, then 1 s, with a 250 ms gap; the first gap is ignored.
    a = wav(24_000, 12_000, fill=1)
    b = wav(24_000, 24_000, extra=True)
    joined = media.join([("a", a, 900), ("b", b, 250)])
    assert wav_ms(joined) == 1_750
    with pytest.raises(ValueError, match="no clips"):
        media.join([])
    with pytest.raises(ValueError, match="format differs"):
        media.join([("a", wav(24_000, 10), 0), ("o", wav(16_000, 10), 0)])
    with pytest.raises(ValueError, match="not a PCM WAV"):
        media.join([("x", b"RIFF", 0)])


def test_a_title_becomes_a_safe_file_name() -> None:
    assert media.file_name_of('Reefs: "a" story/2') == "Reefs a story 2"
    assert media.file_name_of("  ") == "Audio overview"
    assert media.file_name_of("Coral-reefs 101") == "Coral-reefs 101"


def test_the_etag_tracks_the_body() -> None:
    assert media.etag_for(b"same") == media.etag_for(b"same")
    assert media.etag_for(b"same") != media.etag_for(b"different")
    tag = media.etag_for(b"x")
    assert tag.startswith('"') and tag.endswith('"')


def test_one_of_several_offered_tags_matches() -> None:
    tag = media.etag_for(b"body")
    assert media.fresh(tag, tag)
    assert media.fresh(f'"other", {tag}', tag)
    assert not media.fresh('"old"', tag)
    assert not media.fresh(None, tag)
    assert not media.fresh("", tag)


def test_scripts_do_not_survive_a_thumbnail() -> None:
    assert media.strip_scripts("<p>before</p><script>alert(1)</script><p>after</p>") == (
        "<p>before</p><p>after</p>"
    )
    # Case and attributes are not a way past it.
    assert media.strip_scripts("<SCRIPT src=x>y</SCRIPT>a") == "a"
    # An unclosed script takes the remainder with it.
    assert media.strip_scripts("<p>ok</p><script>x") == "<p>ok</p>"
    keep = '<div class="s"><img src="data:image/png;base64,AA"></div>'
    assert media.strip_scripts(keep) == keep


def test_a_slide_ref_is_three_plain_names() -> None:
    storage.put("decks/d/p/s.html", b"<p>one</p>")
    assert media.render_of({"collection": "d", "presentation": "p", "slide": "s"}) == (
        "text/html; charset=utf-8",
        b"<p>one</p>",
    )
    for bad in (
        {"collection": "..", "presentation": "p", "slide": "s"},
        {"collection": "d", "presentation": "../d/p", "slide": "s"},
        {"collection": "d", "presentation": "p"},
        {"collection": "d", "presentation": "p", "slide": "a\\b"},
        "decks/d/p/s.html",
        None,
    ):
        assert media.render_of(bad) is None, bad


def test_an_older_deck_is_read_from_its_output_folder() -> None:
    storage.put("decks/d/p/s/output/slide.png", b"\x89PNG")
    assert media.render_of({"collection": "d", "presentation": "p", "slide": "s"}) == (
        "image/png",
        b"\x89PNG",
    )
    storage.put("decks/d/p/s/output/slide.html", b"<p>html first</p>")
    found = media.render_of({"collection": "d", "presentation": "p", "slide": "s"})
    assert found is not None and found[0].startswith("text/html")


# ── through the routes ────────────────────────────────────────────────────────


async def _collection(client: AsyncClient, headers: dict[str, str] | None = None) -> str:
    r = await client.post("/api/collections", json={"title": "zz media"}, headers=headers)
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _me(client: AsyncClient, headers: dict[str, str] | None = None) -> str:
    return (await client.get("/api/me", headers=headers)).json()["id"]


async def _output(
    owner: str,
    cid: str,
    *,
    kind: str = "slides",
    title: str = "Reefs",
    voiced: bool = True,
) -> str:
    """A ready output written directly, with its files in the Rust layout:
    two slides of two lines each, two speakers."""
    sid = str(uuid.uuid4())
    slides: list[dict[str, Any]] = []
    for o in range(2):
        lines: list[dict[str, Any]] = []
        for k in range(2):
            line_id = f"s{o}l{k}"
            lines.append(
                {
                    "line_id": line_id,
                    "ordinal": k,
                    "speaker_id": f"spk{k}",
                    "text": f"Line {k} of slide {o}.",
                    "duration_ms": 500,
                    "audio_path": f"audio/{sid}/{line_id}.wav" if voiced else None,
                }
            )
            if voiced:
                storage.put(f"audio/{sid}/{line_id}.wav", wav(24_000, 12_000, fill=o + 1))
        slides.append(
            {
                "ordinal": o,
                "title": f"Part {o}",
                "aspect": {"width": 1920, "height": 1080},
                "slide_ref": {"collection": sid, "presentation": "studio", "slide": f"s{o}"},
                "lines": lines,
            }
        )
        storage.put(
            f"decks/{sid}/studio/s{o}.html",
            f"<p>slide {o}</p><script>go()</script>".encode(),
        )
    async with engine().begin() as c:
        await c.execute(
            text(
                "INSERT INTO sessions (id, owner_id, collection_id, kind, title, state, slides,"
                " speakers) VALUES (:id, :o, :c, :k, :t, 'ready', CAST(:s AS jsonb),"
                " CAST(:sp AS jsonb))"
            ),
            {
                "id": sid,
                "o": owner,
                "c": cid,
                "k": kind,
                "t": title,
                "s": json.dumps(slides),
                "sp": json.dumps(
                    [
                        {"speaker_id": "spk0", "voice_id": "af_bella", "display_name": "Host"},
                        {"speaker_id": "spk1", "voice_id": "am_adam", "display_name": "Expert"},
                    ]
                ),
            },
        )
    return sid


async def test_a_line_plays_for_its_owner_only(client: AsyncClient) -> None:
    cid = await _collection(client)
    sid = await _output(await _me(client), cid)

    r = await client.get(f"/api/sessions/{sid}/audio/s1l0")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers["cache-control"] == "private, max-age=3600"
    assert r.headers["accept-ranges"] == "bytes"
    assert r.content == storage.read(f"audio/{sid}/s1l0.wav")

    them = await other_person(client, "media-other@example.com")
    r = await client.get(f"/api/sessions/{sid}/audio/s1l0", headers=them)
    assert r.status_code == 404
    assert r.json()["detail"] == "That output is no longer there. Reload the page to see what is."


async def test_a_line_answers_a_byte_range(client: AsyncClient) -> None:
    cid = await _collection(client)
    sid = await _output(await _me(client), cid)
    whole = storage.read(f"audio/{sid}/s0l0.wav")

    r = await client.get(f"/api/sessions/{sid}/audio/s0l0", headers={"Range": "bytes=0-43"})
    assert r.status_code == 206
    assert r.content == whole[:44]
    assert r.headers["content-range"] == f"bytes 0-43/{len(whole)}"
    r = await client.get(f"/api/sessions/{sid}/audio/s0l0", headers={"Range": "bytes=-4"})
    assert r.status_code == 206 and r.content == whole[-4:]
    r = await client.get(f"/api/sessions/{sid}/audio/s0l0", headers={"Range": "bytes=100-"})
    assert r.status_code == 206 and r.content == whole[100:]
    r = await client.get(
        f"/api/sessions/{sid}/audio/s0l0", headers={"Range": f"bytes={len(whole)}-"}
    )
    assert r.status_code == 416
    r = await client.get(f"/api/sessions/{sid}/audio/s0l0", headers={"Range": "lines=1-2"})
    assert r.status_code == 200 and r.content == whole


async def test_a_line_that_cannot_play_says_why(client: AsyncClient, files: Path) -> None:
    cid = await _collection(client)
    me = await _me(client)
    sid = await _output(me, cid)

    r = await client.get(f"/api/sessions/{sid}/audio/nope")
    assert r.status_code == 404
    assert r.json()["detail"].startswith("That part of the narration is no longer there.")

    (files / f"audio/{sid}/s0l1.wav").unlink()
    r = await client.get(f"/api/sessions/{sid}/audio/s0l1")
    assert r.status_code == 500
    assert "missing from the studio's files" in r.json()["detail"]

    quiet = await _output(me, cid, voiced=False)
    r = await client.get(f"/api/sessions/{quiet}/audio/s0l0")
    assert r.status_code == 404
    assert r.json()["detail"].startswith("This part has no audio yet")

    r = await client.get(f"/api/sessions/{uuid.uuid4()}/audio/s0l0")
    assert r.status_code == 404


async def test_an_episode_is_every_line_with_pauses(client: AsyncClient) -> None:
    cid = await _collection(client)
    sid = await _output(await _me(client), cid, kind="audio", title="Reefs: a story")

    r = await client.get(f"/api/sessions/{sid}/episode")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "audio/wav"
    assert r.headers["content-disposition"] == 'attachment; filename="Reefs a story.wav"'
    # Four lines of 500 ms; spk0 then spk1 on each slide: a turn (220), a new
    # chapter (650), a turn (220).
    assert wav_ms(r.content) == 4 * 500 + 220 + 650 + 220


async def test_an_episode_not_yet_voiced_says_so(client: AsyncClient) -> None:
    cid = await _collection(client)
    sid = await _output(await _me(client), cid, kind="audio", voiced=False)
    r = await client.get(f"/api/sessions/{sid}/episode")
    assert r.status_code == 404
    assert r.json()["detail"].startswith("The narration is not recorded yet.")


async def test_a_slide_is_served_with_an_etag_and_revalidates(client: AsyncClient) -> None:
    cid = await _collection(client)
    sid = await _output(await _me(client), cid)

    r = await client.get(f"/api/sessions/{sid}/slides/1")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "text/html; charset=utf-8"
    assert r.text == "<p>slide 1</p><script>go()</script>", "the slide runs as it was made"
    tag = r.headers["etag"]
    assert r.headers["cache-control"] == "private, max-age=3600"

    r = await client.get(f"/api/sessions/{sid}/slides/1", headers={"If-None-Match": tag})
    assert r.status_code == 304 and r.content == b""
    assert r.headers["etag"] == tag
    r = await client.get(
        f"/api/sessions/{sid}/slides/1", headers={"If-None-Match": f'"old", {tag}'}
    )
    assert r.status_code == 304
    r = await client.get(f"/api/sessions/{sid}/slides/1", headers={"If-None-Match": '"old"'})
    assert r.status_code == 200 and r.headers["etag"] == tag

    r = await client.get(f"/api/sessions/{sid}/slides/7")
    assert r.status_code == 404
    assert r.json()["detail"].startswith("That slide is no longer there.")


async def test_a_thumbnail_has_no_scripts_and_never_fails(client: AsyncClient, files: Path) -> None:
    cid = await _collection(client)
    sid = await _output(await _me(client), cid)

    r = await client.get(f"/api/sessions/{sid}/slides/0", params={"thumb": "1"})
    assert r.status_code == 200 and r.text == "<p>slide 0</p>"
    assert r.headers["etag"] != (await client.get(f"/api/sessions/{sid}/slides/0")).headers["etag"]
    r = await client.get(f"/api/sessions/{sid}/slides/0", params={"thumb": "false"})
    assert "<script>" in r.text

    r = await client.get(f"/api/sessions/{sid}/slides/9", params={"thumb": "1"})
    assert r.status_code == 200 and "no slide" in r.text
    (files / f"decks/{sid}/studio/s1.html").unlink()
    r = await client.get(f"/api/sessions/{sid}/slides/1", params={"thumb": "1"})
    assert r.status_code == 200 and "slides unavailable" in r.text
    r = await client.get(f"/api/sessions/{sid}/slides/1")
    assert r.status_code == 404
    assert "missing from the studio's files" in r.json()["detail"]


async def test_a_share_serves_only_the_outputs_it_includes(client: AsyncClient) -> None:
    cid = await _collection(client)
    me = await _me(client)
    shown = await _output(me, cid, kind="audio")
    hidden = await _output(me, cid)
    r = await client.post(
        f"/api/collections/{cid}/shares",
        json={"include_sources": False, "outputs": [f"session:{shown}"]},
    )
    assert r.status_code == 200, r.text
    share = r.json()["id"]
    them = await other_person(client, "media-viewer@example.com")
    base = f"/api/shares/{share}/sessions"

    r = await client.get(f"{base}/{shown}/audio/s0l1", headers=them)
    assert r.status_code == 200 and r.content == storage.read(f"audio/{shown}/s0l1.wav")
    r = await client.get(f"{base}/{shown}/episode", headers=them)
    assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    r = await client.get(f"{base}/{shown}/slides/0", headers=them)
    assert r.status_code == 200 and r.text.startswith("<p>slide 0</p>")
    r = await client.get(
        f"{base}/{shown}/slides/0", headers={**them, "If-None-Match": r.headers["etag"]}
    )
    assert r.status_code == 304

    # What the share leaves out is as not there, through every byte route.
    gone = "That output is no longer there. Reload the page to see what is."
    for path in (f"{hidden}/audio/s0l0", f"{hidden}/episode", f"{hidden}/slides/0"):
        r = await client.get(f"{base}/{path}", headers=them)
        assert r.status_code == 404, path
        assert r.json()["detail"] == gone
    # And the owner's own routes stay the owner's.
    r = await client.get(f"/api/sessions/{shown}/audio/s0l1", headers=them)
    assert r.status_code == 404

    # A share stopped serves nothing.
    assert (await client.delete(f"/api/shares/{share}")).status_code == 204
    r = await client.get(f"{base}/{shown}/audio/s0l1", headers=them)
    assert r.status_code == 404
    assert r.json()["detail"].startswith("That shared collection is no longer there.")


async def test_an_offset_is_kept_exactly_and_per_output(client: AsyncClient) -> None:
    """playback.rs: the exact millisecond, not a line boundary, and two outputs
    never share a playhead."""
    cid = await _collection(client)
    me = await _me(client)
    a, b = await _output(me, cid), await _output(me, cid)
    at = {"slide_ordinal": 1, "line_id": "s1l1", "offset_ms": 3_477, "state": "paused"}
    assert (await client.put(f"/api/sessions/{a}/playback", json=at)).status_code == 200
    assert (await client.get(f"/api/sessions/{a}/playback")).json() == at
    fresh = (await client.get(f"/api/sessions/{b}/playback")).json()
    assert fresh == {"slide_ordinal": 0, "line_id": "", "offset_ms": 0, "state": "idle"}
