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

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Header, Query, Response
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import MalformedRangeHeader

from opennotebook import storage
from opennotebook.api.deps import SANDBOXED, Db, Me
from opennotebook.build import wav
from opennotebook.build.errors import NotWav
from opennotebook.build.slides import render_of as render_of
from opennotebook.build.wav import gap_before as gap_before
from opennotebook.build.wav import pcm_of as pcm_of
from opennotebook.db.models import Session
from opennotebook.domain import shares
from opennotebook.domain.sessions import Part
from opennotebook.errors import Problem, not_found

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["media"])

# A line's audio and a slide's render never change once written: a rebuild
# writes a new output rather than new bytes under the old one. An hour is the
# number the Rust server chose, and it bounds a Retry that reuses an id.
CACHE = "private, max-age=3600"

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


MISSING_AUDIO = (
    "This output's audio is missing from the studio's files. Make the output again to record it."
)


def _bytes(rel: str) -> bytes:
    """A file the output's row names, or a sentence saying it is missing."""
    try:
        return storage.read(rel)
    except (OSError, ValueError) as e:
        raise Problem(500, MISSING_AUDIO) from e


async def _file(rel: str) -> Path:
    """Where a file the output's row names is, checked to be there, or a
    sentence saying it is missing."""
    try:
        path = storage.local_path(rel)
    except ValueError as e:
        raise Problem(500, MISSING_AUDIO) from e
    if not await asyncio.to_thread(path.is_file):
        raise Problem(500, MISSING_AUDIO)
    return path


def _audio_path(o: Session, line_id: str) -> str:
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
    return path


# ── a line ────────────────────────────────────────────────────────────────────


class Ranged(FileResponse):
    """A file sent from disk as it is read, whole or in the byte ranges asked
    for: a browser seeks in audio with ranges, and some will not seek at all
    without them. A range in a unit other than bytes, or one that cannot be
    read, is ignored and the whole file sent, as HTTP says it may be."""

    @classmethod
    def _parse_range_header(cls, http_range: str, file_size: int) -> list[tuple[int, int]]:
        try:
            return super()._parse_range_header(http_range, file_size)
        except MalformedRangeHeader:
            return []


async def line_response(o: Session, line_id: str) -> Response:
    # Only the path is read here; the bytes are sent once the request's
    # transaction has ended.
    path = await _file(_audio_path(o, line_id))
    return Ranged(path, media_type="audio/wav", headers={"Cache-Control": CACHE})


@router.get("/sessions/{sid}/audio/{line_id}", response_class=Response, responses=WAV)
async def line_audio(sid: uuid.UUID, line_id: str, s: Db, me: Me) -> Response:
    """One narration line's audio, as the player plays it."""
    return await line_response(await _owned(s, me.id, sid), line_id)


@router.get(
    "/shares/{share_id}/sessions/{sid}/audio/{line_id}", response_class=Response, responses=WAV
)
async def shared_line_audio(
    share_id: uuid.UUID, sid: uuid.UUID, line_id: str, s: Db, me: Me
) -> Response:
    """One narration line's audio, of a deck or audio overview a share
    includes."""
    return await line_response(await shares.output_of(s, share_id, Session, sid), line_id)


# ── the episode ───────────────────────────────────────────────────────────────


def join(clips: Sequence[tuple[str, bytes, int]]) -> bytes:
    """Several WAVs as one, each with its own silence in front of it: the
    build's join (`wav.join`), its refusal as a `ValueError` naming why."""
    try:
        return wav.join(list(clips))
    except NotWav as e:
        raise ValueError(str(e)) from e


def file_name_of(title: str) -> str:
    """A title as a download's file name: letters, digits, spaces and dashes
    only, so it sits inside a quoted header value."""
    clean = "".join(c if c.isalnum() or c in " -" else " " for c in title)
    return " ".join(clean.split()) or "Audio overview"


def episode_response(o: Session) -> Response:
    # The episode's plan is the build's (`wav.episode_plan`), so the pauses
    # here are the ones a video of this output is timed on.
    parts = [Part.of_json(p) for p in _slides(o)]
    try:
        plan = wav.episode_plan(parts)
    except ValueError as e:
        raise Problem(
            404,
            "The narration is not recorded yet. "
            "Wait for the output to finish, then download it again.",
        ) from e
    if not plan:
        raise Problem(404, "This output has no narration to download. Make it again to record one.")
    try:
        wav_bytes = join([(line_id, _bytes(path), gap) for line_id, path, gap in plan])
    except ValueError as e:
        log.warning("the episode of %s could not be joined: %s", o.id, e)
        raise Problem(
            500,
            "The episode could not be put together: its recorded lines do not fit together. "
            "Make the output again to re-record it.",
        ) from e
    # Kept to ASCII: a header value is Latin-1, and a title in another script
    # would otherwise fail the whole download.
    name = file_name_of(file_name_of(o.title).encode("ascii", "ignore").decode())
    return Response(
        wav_bytes,
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
    # Reading and joining every line is file work: off the event loop.
    return await asyncio.to_thread(episode_response, await _owned(s, me.id, sid))


@router.get("/shares/{share_id}/sessions/{sid}/episode", response_class=Response, responses=WAV)
async def shared_episode(share_id: uuid.UUID, sid: uuid.UUID, s: Db, me: Me) -> Response:
    """The whole episode of an audio overview a share includes."""
    o = await shares.output_of(s, share_id, Session, sid)
    return await asyncio.to_thread(episode_response, o)


# ── a slide ───────────────────────────────────────────────────────────────────


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
    return Response(html, media_type="text/html; charset=utf-8", headers=SANDBOXED)


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
    if kind.startswith("text/html"):
        headers.update(SANDBOXED)
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
    o = await _owned(s, me.id, sid)
    # Reading the render is file work: off the event loop.
    return await asyncio.to_thread(slide_response, o, ordinal, _thumb(thumb), inm)


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
    return await asyncio.to_thread(slide_response, o, ordinal, _thumb(thumb), inm)
