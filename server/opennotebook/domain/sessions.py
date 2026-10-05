"""Decks and audio overviews: what a build makes, what it is called, and how a
row whose build died is told apart from one still being made.

Ported from `opennotebook_server/src/session_impl.rs` (the plan, the shape,
the title, `reconcile` and `settle`) and the audio overview's spec from
`opennotebook_session/src/model.rs`. The pipeline that fills a row is
`build/pipeline.py`; the job that runs it is `jobs/`.
"""

import re
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook import errors
from opennotebook.db.models import Job, Session
from opennotebook.domain import settings as st
from opennotebook.domain import styles

# ── an audio overview's shape ────────────────────────────────────────────────

# NotebookLM's four formats and three lengths; see `docs/audio-overview-spec.md`.
FORMATS = ("deep_dive", "brief", "critique", "debate")
LENGTHS = ("shorter", "default", "longer")
FORMAT_LABELS = {
    "deep_dive": "Deep Dive",
    "brief": "Brief",
    "critique": "Critique",
    "debate": "Debate",
}


# What each format is, in a line under its name in the Create panel.
FORMAT_BLURBS = {
    "deep_dive": "Two hosts in a lively conversation that unpacks your sources",
    "brief": "One host, the key points in about two minutes",
    "critique": "An expert review of your sources, with constructive feedback",
    "debate": "Two hosts argue different sides of what your sources raise",
}
LENGTH_LABELS = {"shorter": "Shorter", "default": "Default", "longer": "Longer"}


def parse_format(s: str) -> str:
    """A format from its id; anything else is the default, Deep Dive."""
    s = s.strip()
    return s if s in FORMATS else "deep_dive"


def parse_length(s: str) -> str:
    s = s.strip()
    return s if s in ("shorter", "longer") else "default"


def format_speakers(f: str) -> int:
    """How many voices a format takes. Brief is one host, as in NotebookLM."""
    return 1 if f == "brief" else 2


def format_lengths(f: str) -> tuple[str, ...]:
    """The lengths a format offers. NotebookLM: all three for Deep Dive,
    Shorter and Default for Critique and Debate, none for Brief."""
    if f == "deep_dive":
        return LENGTHS
    if f == "brief":
        return ("default",)
    return ("shorter", "default")


@dataclass(frozen=True)
class AudioSpec:
    """What makes an output an audio overview: its format, length and focus."""

    format: str
    length: str
    # What the person asked it to centre on, in their own words: NotebookLM's
    # Customize box. Empty when nothing.
    focus: str = ""

    @property
    def label(self) -> str:
        return FORMAT_LABELS[self.format]

    def real_length(self) -> str:
        """The length it really gets: one its format does not offer falls back
        to Default."""
        return self.length if self.length in format_lengths(self.format) else "default"

    def said(self) -> str:
        """Its format and length as a person reads them: "Brief", "Deep Dive ·
        shorter". The default length goes unsaid."""
        n = self.real_length()
        return self.label if n == "default" else f"{self.label} · {n}"

    def minutes(self) -> int:
        """About how many minutes of audio. Brief is "under two minutes"; a
        Deep Dive defaults near NotebookLM's typical ten to twelve."""
        f, n = self.format, self.real_length()
        if f == "brief":
            return 2
        if f == "deep_dive":
            return {"shorter": 5, "default": 10, "longer": 16}[n]
        return 5 if n == "shorter" else 8

    def chapters(self) -> int:
        """How many chapters. Each one is a part of the plan with its own
        points."""
        f, n = self.format, self.real_length()
        if f == "brief":
            return 2
        if n == "shorter":
            return 3
        if f == "deep_dive":
            return 6 if n == "longer" else 5
        return 4

    def as_json(self) -> dict[str, Any]:
        return {
            "format": self.format,
            "length": self.real_length(),
            "focus": self.focus,
            "minutes": self.minutes(),
        }

    @classmethod
    def of_json(cls, v: dict[str, Any] | None) -> AudioSpec | None:
        if not v:
            return None
        return audio_spec(
            str(v.get("format", "")), str(v.get("length", "")), str(v.get("focus", ""))
        )


def audio_spec(fmt: str | None, length: str | None, focus: str | None) -> AudioSpec | None:
    """An audio overview's spec from a request's three optional fields; None
    when no format is named, which is a slide session."""
    if fmt is None or not fmt.strip():
        return None
    return AudioSpec(parse_format(fmt), parse_length(length or ""), (focus or "").strip())


# ── speakers ─────────────────────────────────────────────────────────────────

# Role words that are not names. A session's speakers used to reach the
# screen as "Host" and "Expert": role words from the settings defaults, shown
# beside every line as if they were names. A listener hears a person, so the
# page shows one. (`opennotebook_sdk/src/voices.rs`)
GENERIC = frozenset(
    {
        "host",
        "expert",
        "narrator",
        "speaker",
        "speaker 1",
        "speaker 2",
        "voice",
        "presenter",
        "guest",
        "studio",
    }
)


# A Microsoft voice's short name, its first name the capitalised word after
# the locale.
_MICROSOFT_NAME = re.compile(r"[a-z]{2,3}-[A-Z]{2}(?:-[a-z]+)?-([A-Z][a-z]+)[A-Za-z]*Neural")


def voice_name(voice_id: str) -> str:
    """The first name a voice id carries: Kokoro's `af_bella` is Bella and
    Microsoft's `en-US-AvaMultilingualNeural` is Ava. Empty for an id that
    does not have one."""
    if m := _MICROSOFT_NAME.fullmatch(voice_id):
        return m.group(1)
    _, sep, name = voice_id.partition("_")
    if not sep or not name:
        return ""
    return name[0].upper() + name[1:]


def is_generic(name: str) -> bool:
    """True for an empty name or a role word standing in for one."""
    n = name.strip().lower()
    return not n or n in GENERIC


def display_name(name: str, voice_id: str) -> str:
    """What to call a speaker: their own name, or their voice's when they
    have only a role word. Falls back to what was given when the voice has
    no name."""
    if is_generic(name) and (v := voice_name(voice_id)):
        return v
    return name.strip()


@dataclass(frozen=True)
class Speaker:
    """One voice of an output. Every line names one by `speaker_id`, a
    one-speaker session included."""

    speaker_id: str
    voice_id: str = ""
    display_name: str = ""
    # Free text; the prompts show it.
    role: str = ""

    def as_json(self) -> dict[str, str]:
        return {
            "speaker_id": self.speaker_id,
            "voice_id": self.voice_id,
            "display_name": self.display_name,
            "role": self.role,
        }

    @classmethod
    def of_json(cls, v: dict[str, Any]) -> Speaker:
        return cls(
            str(v.get("speaker_id", "")),
            str(v.get("voice_id", "")),
            str(v.get("display_name", "")),
            str(v.get("role", "")),
        )

    def named(self) -> Speaker:
        """With a person's name rather than a role word; see `display_name`."""
        return Speaker(
            self.speaker_id,
            self.voice_id,
            display_name(self.display_name, self.voice_id),
            self.role,
        )


# ── the plan: what a build makes ─────────────────────────────────────────────


@dataclass(frozen=True)
class Shape:
    """What a prep builds once its defaults are applied: how many parts, how
    many voices, and whether it is an audio overview.

    The one rule for it. The pipeline builds this, and the spending limit and
    the estimate price this, so the price cannot describe a different build.
    """

    # Slides, or an audio overview's chapters.
    slides: int
    speakers: int
    audio: AudioSpec | None = None


def shape(slide_count: int | None, speakers: int, audio: AudioSpec | None) -> Shape:
    """The shape of a build from its three deciding values. An audio
    overview's format sets its chapters and caps its voices; a slide session
    takes the count it was given, clamped into the settings' range, or the
    default."""
    if audio is not None:
        slides, speakers = audio.chapters(), min(speakers, format_speakers(audio.format))
    else:
        slides = st.clamp_slides(st.SLIDES_DEFAULT if slide_count is None else slide_count)
    return Shape(slides, max(speakers, 1), audio)


@dataclass(frozen=True)
class BuildDefaults:
    """The studio settings a build plan reads, read once per plan."""

    # `auto`, `1` or `2`.
    speaker_count: str
    host: Speaker
    second: Speaker
    # Already in the slide range.
    slide_count: int
    style: str
    audio_format: str
    audio_length: str

    @classmethod
    async def read(cls, s: AsyncSession, owner: uuid.UUID) -> BuildDefaults:
        v = await st.values(s, owner)
        try:
            count = int(v[st.SLIDE_COUNT_KEY].strip())
        except ValueError:
            count = st.SLIDES_DEFAULT
        return cls(
            speaker_count=v[st.SPEAKER_COUNT_KEY],
            host=Speaker(
                "host", v[st.SPEAKER1_VOICE_KEY], v[st.SPEAKER1_NAME_KEY], v[st.SPEAKER1_ROLE_KEY]
            ),
            second=Speaker(
                "expert",
                v[st.SPEAKER2_VOICE_KEY],
                v[st.SPEAKER2_NAME_KEY],
                v[st.SPEAKER2_ROLE_KEY],
            ),
            slide_count=st.clamp_slides(count),
            style=v[st.STYLE_KEY],
            audio_format=v[st.AUDIO_FORMAT_KEY],
            audio_length=v[st.AUDIO_LENGTH_KEY],
        )


@dataclass(frozen=True)
class Ask:
    """What a build request asks for, before the defaults fill the gaps."""

    speakers: int | None = None
    slide_count: int | None = None
    style: str | None = None
    audio_format: str | None = None
    audio_length: str | None = None
    focus: str | None = None
    # How many sources it is built from, for the automatic speaker count.
    sources: int = 0


@dataclass(frozen=True)
class Planned:
    """A request with the defaults applied."""

    speakers: list[Speaker]
    slide_count: int
    style: str
    audio: AudioSpec | None

    def shape(self) -> Shape:
        return shape(self.slide_count, len(self.speakers), self.audio)


class PlanError(ValueError):
    """A request that cannot be built as asked, in a sentence."""


def plan(ask: Ask, d: BuildDefaults) -> Planned:
    """The one rule for what a build makes: speakers, voices, slide count,
    style and an audio overview's length, from the request where it says and
    the settings where it does not.

    A deck's slide count is clamped into the settings' range (3 to 12) whoever
    asks, so an agent's request and the dialog's build the same thing.
    """
    length = (ask.audio_length or "").strip() or d.audio_length
    audio = audio_spec(ask.audio_format, length, ask.focus)
    if audio is not None:
        # An audio overview's format decides its voices: Brief is one host,
        # the rest are two.
        two = format_speakers(audio.format) == 2
    elif ask.speakers is None:
        # A conversation needs something to converse about, and one source
        # rarely carries two voices.
        two = {"1": False, "2": True}.get(d.speaker_count.strip(), ask.sources >= 2)
    elif ask.speakers in (1, 2):
        two = ask.speakers == 2
    else:
        raise PlanError(f"An output has one or two speakers, not {ask.speakers}. Pick 1 or 2.")
    speakers = [d.host, d.second] if two else [d.host]
    if audio is not None:
        slide_count = audio.chapters()
    elif ask.slide_count is not None:
        slide_count = st.clamp_slides(ask.slide_count)
    else:
        slide_count = d.slide_count
    want = (ask.style or "").strip()
    if want and styles.style(want) is None:
        raise PlanError(
            f"“{want}” is not a slide style. Pick one of the styles listed in the Create panel."
        )
    return Planned(speakers, slide_count, want or d.style, audio)


def display_title(title: str, audio: bool) -> str:
    """An output's name as it is shown: its title, or what it is while it has
    none, "Untitled audio overview"."""
    return " ".join(title.split()) or (
        "Untitled audio overview" if audio else "Untitled narrated slides"
    )


# What a failed output says when it has no sentence of its own: one that
# failed before the studio worded its failures, or that wrote nothing.
FAILED_PLAIN = (
    "Something went wrong while making this. Your sources are kept, so you can try again "
    "or change them."
)
_SENTENCE = re.compile(r"[A-Z][^{}<>]*[.!?]")
_HTTP = re.compile(r"\bHTTP \d{3}\b")


def failure_said(raw: str | None, detail: str | None) -> tuple[str, str | None]:
    """Why an output failed, as a person reads it, and the technical detail
    kept apart for whoever is debugging. A build writes a sentence, which is
    said as it is, with its job's own words as the detail when they differ.
    An older row whose reason is raw words gets the known sentence for them,
    else a plain one, and keeps the raw words as the detail."""
    r = (raw or "").strip()
    d = (detail or "").strip() or None
    if not r:
        return FAILED_PLAIN, d
    if _SENTENCE.fullmatch(r) and not _HTTP.search(r):
        return r, None if d == r else d
    return errors.known(r) or FAILED_PLAIN, r


def one_line(s: str) -> str:
    """Control characters out, ends trimmed; a newline is dropped, not turned
    into a space, as the Rust server did."""
    return "".join(c for c in s if c.isprintable() or c == " ").strip()


def clip_title(s: str) -> str:
    """A title cut to 72 characters at a word, never below 24."""
    s = s.strip().strip('"').strip()
    if len(s) <= 72:
        return s
    cut = s[:72]
    head, sep, _ = cut.rpartition(" ")
    if sep and len(head) >= 24:
        return head.rstrip()
    return cut.rstrip()


def output_title(given: str, style: str | None, audio: AudioSpec | None) -> str:
    """An output's title: the one given, else one that tells it from the
    others in its collection — "Editorial slides", "Brief audio overview ·
    <focus>". Never empty: an untitled card cannot be found in a list sorted
    by title."""
    given = one_line(given)
    if given:
        return given
    if audio is not None and not audio.focus.strip():
        named = f"{audio.label} audio overview"
    elif audio is not None:
        named = f"{audio.label} audio overview · {one_line(audio.focus)}"
    else:
        found = styles.style(style or "") or styles.DEFAULT_STYLE
        named = f"{found.label} slides"
    return clip_title(named)


# ── the stored parts ─────────────────────────────────────────────────────────


@dataclass
class Line:
    """One spoken line. `audio_path` and `duration_ms` are set once it is
    voiced; a duration only ever comes from the WAV that will play."""

    line_id: str
    speaker_id: str
    ordinal: int
    text: str
    audio_path: str | None = None
    duration_ms: int | None = None
    cues: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])

    def as_json(self) -> dict[str, Any]:
        return {
            "line_id": self.line_id,
            "speaker_id": self.speaker_id,
            "ordinal": self.ordinal,
            "text": self.text,
            "audio_path": self.audio_path,
            "duration_ms": self.duration_ms,
            "cues": self.cues,
        }

    @classmethod
    def of_json(cls, v: dict[str, Any]) -> Line:
        d = v.get("duration_ms")
        return cls(
            str(v.get("line_id", "")),
            str(v.get("speaker_id", "")),
            int(v.get("ordinal", 0)),
            str(v.get("text", "")),
            v.get("audio_path"),
            int(d) if d is not None else None,
            list(v.get("cues") or []),
        )


@dataclass
class Part:
    """A slide of a deck, or a chapter of an audio overview: its title, the
    copy printed on it, and its lines. Stored with the same field names the
    Rust store used, so the player reads either."""

    # `slide_ref.slide`: a snake_case name, the slide's file name in the deck.
    slide: str
    ordinal: int
    title: str
    on_slide: list[str]
    lines: list[Line]
    collection: str = ""
    presentation: str = ""
    width: int = 1920
    height: int = 1080

    def lines_in_order(self) -> list[Line]:
        return sorted(self.lines, key=lambda line: line.ordinal)

    def as_json(self) -> dict[str, Any]:
        return {
            "slide_ref": {
                "collection": self.collection,
                "presentation": self.presentation,
                "slide": self.slide,
            },
            "ordinal": self.ordinal,
            "aspect": {"width": self.width, "height": self.height},
            "title": self.title,
            "on_slide": self.on_slide,
            "lines": [line.as_json() for line in self.lines],
        }

    @classmethod
    def of_json(cls, v: dict[str, Any]) -> Part:
        ref: dict[str, Any] = v.get("slide_ref") or {}
        aspect: dict[str, Any] = v.get("aspect") or {}
        return cls(
            slide=str(ref.get("slide", "")),
            ordinal=int(v.get("ordinal", 0)),
            title=str(v.get("title", "")),
            on_slide=[str(e) for e in v.get("on_slide") or []],
            lines=[Line.of_json(line) for line in v.get("lines") or []],
            collection=str(ref.get("collection", "")),
            presentation=str(ref.get("presentation", "")),
            width=int(aspect.get("width", 1920)),
            height=int(aspect.get("height", 1080)),
        )


def in_order(parts: list[Part]) -> list[Part]:
    return sorted(parts, key=lambda p: p.ordinal)


def duration_of(parts: list[Part]) -> int:
    """The measured narration length: every voiced line's duration, summed."""
    return sum(line.duration_ms or 0 for p in parts for line in p.lines)


def first_line(parts: list[Part]) -> str:
    """The first spoken line, as a preview of the output."""
    for p in in_order(parts):
        lines = p.lines_in_order()
        if lines:
            return lines[0].text
    return ""


# ── a row whose build died ───────────────────────────────────────────────────

# A job that stopped and will never write its output's outcome.
DEAD = ("failed", "cancelled", "aborted", "inactive")


def stopped_sentence(status: str) -> str:
    """Why a row whose job died says failed, for a person."""
    if status in ("cancelled", "aborted"):
        return "This build was stopped before it finished. Start it again to make it."
    return "This build stopped before it finished, so nothing was made. Try again."


def settle(state_now: str | None, status: str, collection_wanted: bool) -> tuple[str, bool]:
    """What reconciling a `preparing` row whose job stopped comes to, and
    whether it is written: `failed`, written, only onto a row that is still
    there, still `preparing`, and in a collection that is still there. A
    deleted row written back would undelete the output; one the store could
    not read (`state_now` None from a failed read is the caller's to skip)
    is left for the next look.

    `state_now` is the row's state when re-read, None when it is gone.
    """
    if state_now is None:
        return "preparing", False
    if state_now != "preparing":
        return state_now, False
    if not collection_wanted or status not in DEAD:
        return "preparing", False
    return "failed", True


async def job_status(s: AsyncSession, sid: uuid.UUID) -> tuple[Job | None, str]:
    """The newest job of an output and whether its work is still happening:
    our row's status, corrected by the queue's own record of the job. A job
    with no row at all is `inactive`: nothing is doing the work, and nothing
    ever will."""
    return (await job_statuses(s, [sid]))[sid]


async def job_statuses(
    s: AsyncSession, sids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, tuple[Job | None, str]]:
    """`job_status` of every output in `sids`, in two queries however many
    there are: each one's newest job, then the queue's record of those."""
    if not sids:
        return {}
    newest = {
        j.session_id: j
        for j in await s.scalars(
            select(Job)
            .where(Job.session_id.in_(sids))
            .order_by(Job.session_id, Job.created_at.desc())
            .ext(distinct_on(Job.session_id))
        )
    }
    asked = [
        j.procrastinate_job_id
        for j in newest.values()
        if j.procrastinate_job_id is not None and j.status not in ("failed", "cancelled", "done")
    ]
    queued: dict[int, str] = {}
    if asked:
        rows = await s.execute(
            text("SELECT id, status::text FROM procrastinate_jobs WHERE id = ANY(:ids)"),
            {"ids": asked},
        )
        queued = {int(i): q for i, q in rows}
    return {sid: _status(newest.get(sid), queued) for sid in sids}


def _status(job: Job | None, queued: dict[int, str]) -> tuple[Job | None, str]:
    """Whether a job's work is still happening, from its row and the queue's
    statuses by queue id."""
    if job is None:
        return None, "inactive"
    if job.status in ("failed", "cancelled"):
        return job, job.status
    if job.status == "done":
        # Done without writing an outcome: nothing will write one now.
        return job, "failed"
    if job.procrastinate_job_id is None:
        return job, job.status
    q = queued.get(job.procrastinate_job_id)
    if q is None:
        return job, "inactive"
    if q in ("failed", "cancelled", "aborted"):
        return job, q
    if q == "succeeded":
        return job, "failed"
    return job, job.status


# How recently a worker must have reported for the queue to count as served.
# Workers report every ten seconds.
WORKER_SILENT_SECONDS = 30

WAITING = (
    "Waiting for the studio's worker to start. If this does not change, whoever runs the "
    "studio needs to start it with `opennotebook worker`."
)


async def worker_alive(s: AsyncSession) -> bool:
    """Whether any worker has reported in the last `WORKER_SILENT_SECONDS`."""
    return bool(
        await s.scalar(
            text(
                "SELECT EXISTS (SELECT 1 FROM procrastinate_workers WHERE last_heartbeat > "
                "now() - make_interval(secs => :n))"
            ),
            {"n": WORKER_SILENT_SECONDS},
        )
    )


async def waiting(s: AsyncSession, o: Session, status: str | None = None) -> str | None:
    """Why a `preparing` output has not started, when nothing is there to
    start it: its build is queued and no worker is running. Without this it
    would say "Starting" forever. `status` is its job's, when already read."""
    if o.state != "preparing":
        return None
    if status is None:
        _, status = await job_status(s, o.id)
    if status == "queued" and not await worker_alive(s):
        return WAITING
    return None


async def reconcile(s: AsyncSession, o: Session, status: str | None = None) -> Session:
    """A `preparing` row whose job is no longer alive, marked `failed`.

    A prep that errors writes its own failure. One that is killed — the
    worker restarted, the job cancelled, the box rebooted — writes nothing,
    and its row would say `preparing` forever: a card spinning on the gallery
    and a progress screen that never moves. The job row is the authority on
    whether the work is still happening, so it is asked. Anything short of a
    clear "it stopped" (the job still queued or running) leaves the row
    alone. `status` is its job's (`job_statuses`), when already read.
    """
    if o.state != "preparing":
        return o
    if status is None:
        _, status = await job_status(s, o.id)
    if status not in DEAD:
        return o
    # Re-read under its lock before writing: the prep may have recorded its
    # own, better reason between the two reads, or the row may have been
    # deleted. A row that exists has its collection: deleting a collection
    # takes its outputs with it.
    now = await s.scalar(
        select(Session)
        .where(Session.id == o.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    state, write = settle(None if now is None else now.state, status, now is not None)
    if now is None:
        return o
    if write:
        now.state = state
        now.failure = stopped_sentence(status)
    return now
