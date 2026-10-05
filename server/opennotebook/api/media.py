"""The bytes the player plays: a narration line's audio, an audio overview's
whole episode as one WAV, and a slide's render. Ported from the Rust server's
byte routes (`main.rs`: `serve_audio`, `serve_episode`, `serve_slide`).

Each route comes twice. Under `/api/sessions/{sid}` it is the output's
owner's. Under `/api/shares/{share}/sessions/{sid}` anyone signed in may read
it, and only for an output the share includes and that is still there, the
same check every other share route makes: anything else is as not there.

Paths are never taken from the caller. A line's audio is where the output's
own row says it is, and a slide is under the deck its `slide_ref` names, each
part a plain name; the files volume then refuses anything outside itself.
"""

import hashlib
import re
import struct
import uuid
from collections.abc import Sequence
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook import storage
from opennotebook.api.deps import Db, Me
from opennotebook.db.models import Session
from opennotebook.domain import shares
from opennotebook.errors import Problem, not_found

router = APIRouter(prefix="/api", tags=["media"])

# A line's audio and a slide's render never change once written: a rebuild
# writes a new output rather than new bytes under the old one. An hour is the
# number the Rust server chose, and it bounds a Retry that reuses an id.
CACHE = "private, max-age=3600"

# Silence in front of a line in the episode: when the other speaker answers,
# when the same one goes on, and when a new chapter starts. People leave about
# 200 ms between turns in conversation (Stivers et al., 2009); a chapter is a
# breath longer.
GAP_TURN_MS = 220
GAP_SAME_MS = 320
GAP_CHAPTER_MS = 650

WAV: dict[int | str, dict[str, Any]] = {
    200: {"content": {"audio/wav": {}}, "description": "A WAV file"}
}
SLIDE: dict[int | str, dict[str, Any]] = {
    200: {
        "content": {"text/html": {}, "image/png": {}},
        "description": "The slide's own HTML document, or a PNG for a deck drawn as pictures",
    },
    304: {"description": "The copy the browser holds is current"},
}


# ── reading an output ─────────────────────────────────────────────────────────


async def _owned(s: AsyncSession, owner: uuid.UUID, sid: uuid.UUID) -> Session:
    o = await s.scalar(select(Session).where(Session.id == sid, Session.owner_id == owner))
    if o is None:
        raise not_found("That output")
    return o


def _slides(o: Session) -> list[dict[str, Any]]:
    """The parts in play order, each with an ordinal: its own, or its place."""
    out: list[dict[str, Any]] = []
    for i, raw in enumerate(o.slides):
        if isinstance(raw, dict):
            part: dict[str, Any] = dict(raw)  # pyright: ignore[reportUnknownArgumentType]
            part.setdefault("ordinal", i)
            out.append(part)
    return sorted(out, key=lambda p: _int(p.get("ordinal")))


def _lines(part: dict[str, Any]) -> list[dict[str, Any]]:
    raw = part.get("lines")
    lines: list[dict[str, Any]] = [
        {"ordinal": i, **ln}
        for i, ln in enumerate(raw if isinstance(raw, list) else [])  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        if isinstance(ln, dict)
    ]
    return sorted(lines, key=lambda ln: _int(ln.get("ordinal")))


def _int(v: Any) -> int:
    return v if isinstance(v, int) else 0


def _bytes(rel: str) -> bytes:
    """A file the output's row names, or a sentence saying it is missing."""
    try:
        return storage.read(rel)
    except (OSError, ValueError) as e:
        raise Problem(
            500,
            "This output's audio is missing from the studio's files. "
            "Make the output again to record it.",
        ) from e


def _audio_of(o: Session, line_id: str) -> bytes:
    line = next(
        (ln for p in _slides(o) for ln in _lines(p) if ln.get("line_id") == line_id),
        None,
    )
    if line is None:
        raise not_found("That part of the narration")
    path = line.get("audio_path")
    # Absent until the line is voiced: a real state while an output is made,
    # not a fault, so it is said as such.
    if not isinstance(path, str) or not path:
        raise Problem(
            404,
            "This part has no audio yet: the output is still being made. "
            "Wait for it to finish, then press play again.",
        )
    return _bytes(path)


# ── a line ────────────────────────────────────────────────────────────────────

RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _ranged(body: bytes, media_type: str, range_: str | None, headers: dict[str, str]) -> Response:
    """The whole body, or the one byte range asked for: a browser seeks in
    audio with ranges, and some will not seek at all without them."""
    headers = {**headers, "Accept-Ranges": "bytes"}
    m = RANGE.match(range_.strip()) if range_ else None
    if m is None or not body or (m.group(1) == "" and m.group(2) == ""):
        return Response(body, media_type=media_type, headers=headers)
    n = len(body)
    if m.group(1) == "":
        start, end = max(0, n - int(m.group(2))), n - 1
    else:
        start = int(m.group(1))
        end = min(int(m.group(2)), n - 1) if m.group(2) else n - 1
    if start >= n or end < start:
        return Response(status_code=416, headers={**headers, "Content-Range": f"bytes */{n}"})
    headers["Content-Range"] = f"bytes {start}-{end}/{n}"
    return Response(body[start : end + 1], status_code=206, media_type=media_type, headers=headers)


def line_response(o: Session, line_id: str, range_: str | None) -> Response:
    return _ranged(_audio_of(o, line_id), "audio/wav", range_, {"Cache-Control": CACHE})


RangeHeader = Annotated[str | None, Header(alias="Range", include_in_schema=False)]


@router.get("/sessions/{sid}/audio/{line_id}", response_class=Response, responses=WAV)
async def line_audio(
    sid: uuid.UUID, line_id: str, s: Db, me: Me, range_: RangeHeader = None
) -> Response:
    """One narration line's audio, as the player plays it."""
    return line_response(await _owned(s, me.id, sid), line_id, range_)


@router.get(
    "/shares/{share_id}/sessions/{sid}/audio/{line_id}", response_class=Response, responses=WAV
)
async def shared_line_audio(
    share_id: uuid.UUID, sid: uuid.UUID, line_id: str, s: Db, me: Me, range_: RangeHeader = None
) -> Response:
    """One narration line's audio, of a deck or audio overview a share
    includes."""
    return line_response(await shares.output_of(s, share_id, Session, sid), line_id, range_)


# ── the episode ───────────────────────────────────────────────────────────────


def gap_before(first_of_chapter: bool, same_speaker: bool) -> int:
    """The pause in front of a line: a new chapter, the same speaker going
    on, or the other speaker answering."""
    if first_of_chapter:
        return GAP_CHAPTER_MS
    return GAP_SAME_MS if same_speaker else GAP_TURN_MS


def pcm_of(data: bytes) -> tuple[int, int, int, bytes] | None:
    """(channels, sample rate, bits per sample, PCM bytes) of a PCM WAV. The
    chunks are walked, not assumed at fixed offsets: a WAV may carry `LIST`
    or `fact` before `data`."""
    if len(data) < 12 or data[0:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    fmt: tuple[int, int, int, int] | None = None
    at = 12
    while at + 8 <= len(data):
        cid = data[at : at + 4]
        size = struct.unpack_from("<I", data, at + 4)[0]
        body = at + 8
        if cid == b"fmt " and body + 16 <= len(data):
            tag, channels, rate = struct.unpack_from("<HHI", data, body)
            bits = struct.unpack_from("<H", data, body + 14)[0]
            fmt = (tag, channels, rate, bits)
        elif cid == b"data":
            if fmt is None or fmt[0] != 1:
                return None
            return fmt[1], fmt[2], fmt[3], data[body : min(body + size, len(data))]
        at = body + size + (size & 1)
    return None


def join(clips: Sequence[tuple[str, bytes, int]]) -> bytes:
    """Several WAVs as one, each with its own silence in front of it; the
    first clip's gap is ignored. Every clip must share the first one's format,
    or the episode would play part of itself at the wrong speed."""
    fmt: tuple[int, int, int] | None = None
    out = bytearray()
    for k, (name, data, gap_ms) in enumerate(clips):
        pcm = pcm_of(data)
        if pcm is None:
            raise ValueError(f"{name} is not a PCM WAV")
        if fmt is None:
            fmt = pcm[:3]
        elif fmt != pcm[:3]:
            raise ValueError(f"{name} has a different format from the first clip")
        channels, rate, bits = fmt
        if k > 0:
            out += bytes(rate * gap_ms // 1000 * channels * (bits // 8))
        out += pcm[3]
    if fmt is None:
        raise ValueError("there are no clips to join")
    channels, rate, bits = fmt
    block = channels * (bits // 8)
    head = b"RIFF" + struct.pack("<I", 36 + len(out)) + b"WAVEfmt "
    head += struct.pack("<IHHIIHH", 16, 1, channels, rate, rate * block, block, bits)
    return head + b"data" + struct.pack("<I", len(out)) + bytes(out)


def file_name_of(title: str) -> str:
    """A title as a download's file name: letters, digits, spaces and dashes
    only, so it sits inside a quoted header value."""
    clean = "".join(c if c.isalnum() or c in " -" else " " for c in title)
    return " ".join(clean.split()) or "Audio overview"


def episode_response(o: Session) -> Response:
    clips: list[tuple[str, bytes, int]] = []
    prev: str | None = None
    for ci, part in enumerate(_slides(o)):
        for li, line in enumerate(_lines(part)):
            line_id = str(line.get("line_id", ""))
            path = line.get("audio_path")
            if not isinstance(path, str) or not path:
                raise Problem(
                    404,
                    "The narration is not recorded yet. "
                    "Wait for the output to finish, then download it again.",
                )
            speaker = str(line.get("speaker_id", ""))
            clips.append((line_id, _bytes(path), gap_before(ci > 0 and li == 0, prev == speaker)))
            prev = speaker
    if not clips:
        raise Problem(404, "This output has no narration to download. Make it again to record one.")
    try:
        wav = join(clips)
    except ValueError as e:
        raise Problem(
            500,
            f"The episode could not be put together: {e}. Make the output again to re-record it.",
        ) from e
    # Kept to ASCII: a header value is Latin-1, and a title in another script
    # would otherwise fail the whole download.
    name = file_name_of(file_name_of(o.title).encode("ascii", "ignore").decode())
    return Response(
        wav,
        media_type="audio/wav",
        headers={
            "Content-Disposition": f'attachment; filename="{name}.wav"',
            "Cache-Control": CACHE,
        },
    )


@router.get("/sessions/{sid}/episode", response_class=Response, responses=WAV)
async def episode(sid: uuid.UUID, s: Db, me: Me) -> Response:
    """Every line, in order, as one WAV with natural pauses between them: an
    audio overview to download."""
    return episode_response(await _owned(s, me.id, sid))


@router.get("/shares/{share_id}/sessions/{sid}/episode", response_class=Response, responses=WAV)
async def shared_episode(share_id: uuid.UUID, sid: uuid.UUID, s: Db, me: Me) -> Response:
    """The whole episode of an audio overview a share includes."""
    return episode_response(await shares.output_of(s, share_id, Session, sid))


# ── a slide ───────────────────────────────────────────────────────────────────

PLAIN = re.compile(r"^[^/\\]+$")


def etag_for(body: bytes) -> str:
    """A tag that changes when the bytes do, and only then. Not a security
    hash; quoted, as an entity tag must be."""
    return '"' + hashlib.blake2b(body, digest_size=8).hexdigest() + '"'


def fresh(if_none_match: str | None, etag: str) -> bool:
    """Whether the browser already holds these bytes: any one of the tags it
    offers matching is a hit."""
    if not if_none_match:
        return False
    return any(t.strip() == etag for t in if_none_match.split(","))


def strip_scripts(html: str) -> str:
    """Every `<script>` element dropped, contents and all, and everything else
    kept byte for byte. An unclosed one takes the rest with it rather than
    leave a half tag for the browser to repair."""
    lower = html.lower()
    out: list[str] = []
    i = 0
    while (start := lower.find("<script", i)) >= 0:
        out.append(html[i:start])
        end = lower.find("</script>", start)
        if end < 0:
            return "".join(out)
        i = end + len("</script>")
    out.append(html[i:])
    return "".join(out)


def thumb_placeholder(why: str) -> Response:
    """A slide-shaped "nothing here", for a thumbnail that cannot render: a
    thumbnail is decorative, and a missing one must not break a page."""
    safe = why.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html = (
        "<!doctype html><meta charset=utf-8>"
        '<div style="position:fixed;inset:0;display:grid;place-items:center;'
        'background:#0d1219;color:#8b98a9;font:34px ui-sans-serif,system-ui,sans-serif">'
        f"{safe}</div>"
    )
    return Response(html, media_type="text/html; charset=utf-8")


def render_of(slide_ref: Any) -> tuple[str, bytes] | None:
    """A slide's render on the files volume, with its type: the deck's own
    `<presentation>/<slide>.html`, or an older deck's `output/slide.html` or
    `output/slide.png`. None when there is none or the ref is not three plain
    names."""
    if not isinstance(slide_ref, dict):
        return None
    parts = [slide_ref.get(k) for k in ("collection", "presentation", "slide")]  # pyright: ignore[reportUnknownMemberType]
    names = [p for p in parts if isinstance(p, str) and PLAIN.match(p) and ".." not in p]
    if len(names) != 3:
        return None
    deck, pres, slide = names
    base = f"decks/{deck}/{pres}"
    for rel, kind in (
        (f"{base}/{slide}.html", "text/html; charset=utf-8"),
        (f"{base}/{slide}/output/slide.html", "text/html; charset=utf-8"),
        (f"{base}/{slide}/output/slide.png", "image/png"),
    ):
        try:
            if storage.exists(rel):
                return kind, storage.read(rel)
        except OSError, ValueError:
            continue
    return None


def slide_response(o: Session, ordinal: int, thumb: bool, if_none_match: str | None) -> Response:
    part = next((p for p in _slides(o) if _int(p.get("ordinal")) == ordinal), None)
    if part is None:
        if thumb:
            return thumb_placeholder("no slide")
        raise not_found("That slide")
    found = render_of(part.get("slide_ref"))
    if found is None or not found[1]:
        if thumb:
            return thumb_placeholder("slides unavailable")
        raise Problem(
            404,
            "This slide's picture is missing from the studio's files. "
            "Make the deck again to draw it.",
        )
    kind, body = found
    if thumb and kind.startswith("text/html"):
        # A thumbnail is a picture of a slide, not a running one: its frame is
        # sandboxed without scripts, so each one would only be refused and logged.
        body = strip_scripts(body.decode("utf-8", "replace")).encode()
    etag = etag_for(body)
    headers = {"ETag": etag, "Cache-Control": CACHE}
    if fresh(if_none_match, etag):
        return Response(status_code=304, headers=headers)
    return Response(body, media_type=kind, headers=headers)


Thumb = Annotated[
    str,
    Query(
        description="Any value but empty, `0` or `false` asks for a thumbnail: scripts are "
        "removed, and a slide that cannot be drawn is a small placeholder rather than an error"
    ),
]
IfNoneMatch = Annotated[str | None, Header(alias="If-None-Match", include_in_schema=False)]


def _thumb(v: str) -> bool:
    return v not in ("", "0", "false")


@router.get("/sessions/{sid}/slides/{ordinal}", response_class=Response, responses=SLIDE)
async def slide(
    sid: uuid.UUID, ordinal: int, s: Db, me: Me, thumb: Thumb = "", inm: IfNoneMatch = None
) -> Response:
    """One slide of a deck as its own HTML document (or PNG), with an ETag so
    a browser that holds it is answered 304."""
    return slide_response(await _owned(s, me.id, sid), ordinal, _thumb(thumb), inm)


@router.get(
    "/shares/{share_id}/sessions/{sid}/slides/{ordinal}", response_class=Response, responses=SLIDE
)
async def shared_slide(
    share_id: uuid.UUID,
    sid: uuid.UUID,
    ordinal: int,
    s: Db,
    me: Me,
    thumb: Thumb = "",
    inm: IfNoneMatch = None,
) -> Response:
    """One slide of a deck a share includes."""
    o = await shares.output_of(s, share_id, Session, sid)
    return slide_response(o, ordinal, _thumb(thumb), inm)
