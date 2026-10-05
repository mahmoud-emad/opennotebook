"""The event stream of one output: its state, its build's progress, and its
playback. A port of `opennotebook_server/src/events.rs` and the stream in
`player.rs`, now woken by `NOTIFY` instead of a one-second poll.

The names are the old stream's, verbatim, so a player written against them
need not translate:

- `session.state` — `preparing | ready | failed | idle | playing | paused |
  finished`. `failed` is there because a prep screen that cannot say "this
  failed" would spin forever on an output that is never coming.
- `prep.progress` — the step a build is on and how far: `step`,
  `steps_done`, `steps_total`. A job row with `steps_total` 0 does not
  report, so it is not forwarded: a bar drawn from it would be a lie.
- `prep.waiting` — `waiting`: a sentence while the build is queued and no
  worker is running to start it, null once one is.
- `slide.enter`, `line.start`, `line.end`, `playhead` — playback, derived
  from the playhead the player records.

Once an output is `ready` or `failed` its build has nothing left to say; the
stream goes on with playback alone.
"""

import asyncio
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from opennotebook.db.models import Playback, Session
from opennotebook.db.session import sessionmaker
from opennotebook.domain import sessions
from opennotebook.domain.sessions import Part
from opennotebook.jobs.events import REREAD_SECONDS, SESSION_CHANNEL, hub

Event = tuple[str, dict[str, Any]]

# Announces a change to an output's playhead; the payload is the output's id.
SESSION_CHANNEL_SQL = f"SELECT pg_notify('{SESSION_CHANNEL}', :sid)"


@dataclass(frozen=True)
class Head:
    """A playhead, as the player records it."""

    state: str = "idle"
    slide_ordinal: int = 0
    line_id: str = ""
    offset_ms: int = 0


@dataclass(frozen=True)
class Lookup:
    """What playback events need to know about the output's parts."""

    # ordinal -> (collection, presentation, slide)
    slides: dict[int, tuple[str, str, str]]
    # line id -> (speaker id, duration ms, audio url)
    lines: dict[str, tuple[str, int, str]]

    @classmethod
    def of(cls, o: Session) -> Lookup:
        parts = [Part.of_json(p) for p in o.slides]
        return cls(
            {p.ordinal: (p.collection, p.presentation, p.slide) for p in parts},
            {
                line.line_id: (
                    line.speaker_id,
                    line.duration_ms or 0,
                    f"/api/sessions/{o.id}/audio/{line.line_id}",
                )
                for p in parts
                for line in p.lines
            },
        )


def playback_events(was: Head, now: Head, look: Lookup | None) -> list[Event]:
    """The events that describe a move from `was` to `now`, in the order a
    player should see them. Pure, so the ordering is testable without a
    browser."""
    out: list[Event] = []
    if was.state != now.state:
        out.append(("session.state", {"state": now.state}))
    # Order matters and is part of what a player can rely on:
    #
    #   line.end  ->  slide.enter  ->  line.start
    #
    # End the line that was speaking before changing what is on screen, then
    # enter the new slide, then start the line spoken over it. The other order
    # was tried and observed live: `slide.enter` arrived first, so the new
    # slide was on screen for one frame with the previous slide's line still
    # highlighted.
    line_changed = was.line_id != now.line_id
    if line_changed and was.line_id:
        out.append(("line.end", {"line_id": was.line_id}))
    entered = was.slide_ordinal != now.slide_ordinal or (not was.line_id and bool(now.line_id))
    if entered and look is not None and (s := look.slides.get(now.slide_ordinal)) is not None:
        out.append(
            (
                "slide.enter",
                {
                    "slide_ordinal": now.slide_ordinal,
                    "collection": s[0],
                    "presentation": s[1],
                    "slide": s[2],
                },
            )
        )
    if (
        line_changed
        and now.line_id
        and look is not None
        and (ln := look.lines.get(now.line_id)) is not None
    ):
        out.append(
            (
                "line.start",
                {
                    "line_id": now.line_id,
                    # Carried so the page can show who is talking with one
                    # speaker or two.
                    "speaker_id": ln[0],
                    "duration_ms": ln[1],
                    "audio_url": ln[2],
                },
            )
        )
    if was.offset_ms != now.offset_ms or line_changed:
        out.append(
            (
                "playhead",
                {
                    "slide_ordinal": now.slide_ordinal,
                    "line_id": now.line_id,
                    "offset_ms": now.offset_ms,
                },
            )
        )
    return out


async def _read(owner: uuid.UUID, sid: uuid.UUID) -> tuple[Session | None, Any, Head]:
    """The output, its build's progress and its playhead, as they are now. A
    `preparing` row whose job died is reconciled on the way."""
    async with sessionmaker()() as s, s.begin():
        o = await s.scalar(select(Session).where(Session.id == sid, Session.owner_id == owner))
        if o is None:
            return None, (None, None), Head()
        o = await sessions.reconcile(s, o)
        job, _ = await sessions.job_status(s, sid)
        if job is not None:
            hub.follow_job(job.id, sid)
        p = await s.get(Playback, sid)
        head = Head() if p is None else Head(p.state, p.slide_ordinal, p.line_id, p.offset_ms)
        note = await sessions.waiting(s, o)
        progress = (
            None
            if job is None or job.steps_total == 0
            else (job.step, job.steps_done, job.steps_total)
        )
        return o, (progress, note), head


async def stream(owner: uuid.UUID, sid: uuid.UUID) -> AsyncGenerator[Event | None]:
    """The output's events as they happen. None is a keep-alive: nothing
    changed in `REREAD_SECONDS`. Ends when the output is deleted."""
    last_state: str | None = None
    last_progress: Any = None
    last_note: str | None = None
    # The default playhead, not a sentinel. A sentinel was tried and it
    # leaked: `line_id: "\\0never"` made the first diff look like a line had
    # just ended, and the stream's opening frame was a `line.end` for a line
    # that never existed. A default `was` still emits everything a page
    # joining mid-session needs, while an empty previous line cannot produce
    # a `line.end`.
    last_head = Head()
    async with hub.subscribe(sid) as woken:
        while True:
            woken.clear()
            o, (progress, note), head = await _read(owner, sid)
            if o is None:
                return
            out: list[Event] = []
            if o.state != last_state:
                out.append(("session.state", {"state": o.state}))
                last_state = o.state
            # Progress, while there is a build to report on.
            if o.state == "preparing" and progress is not None and progress != last_progress:
                step, done, total = progress
                out.append(
                    ("prep.progress", {"step": step, "steps_done": done, "steps_total": total})
                )
                last_progress = progress
            # Queued with no worker to run it: said, so the page does not
            # show "Starting" forever. `waiting` is null again once it starts.
            if note != last_note:
                out.append(("prep.waiting", {"waiting": note}))
                last_note = note
            if head != last_head:
                out.extend(playback_events(last_head, head, Lookup.of(o)))
                last_head = head
            for e in out:
                yield e
            try:
                async with asyncio.timeout(REREAD_SECONDS):
                    await woken.wait()
            except TimeoutError:
                yield None
