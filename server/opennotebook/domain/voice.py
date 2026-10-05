"""The voice turn: a spoken question in, a spoken answer out. A port of
`opennotebook_server/src/ask.rs`.

# Why this is one call and not a pipeline

The phase 2 spec designed the obvious thing: transcribe the question, ask a
text model, synthesise the answer. Measured end to end that was about **31
seconds**, and 84% of it was local synthesis at 1.1x realtime.

[Moshi](https://arxiv.org/abs/2410.00037) argues the pipeline itself is the
problem rather than any stage of it: "their complexity induces a latency of
several seconds between interactions", and text as the intermediate modality
throws away everything about the speech that was not words.

So the question's audio goes to a model that takes audio in, and the reply is
streamed back as it arrives.

# Audio in, text out, the narrator's own voice

This route used to ask that model for audio OUT as well, and it is worth
writing down why it stopped. No audio-out model takes a Kokoro voice id, so
the answer came back in a provider voice: the narrator said "go ahead" in
their voice, said "let me think about that" in their voice, and then a
stranger answered the question. One turn, two people. That is the most
audible seam this product had.

So the model is asked for text and the answer is synthesised HERE, by the
same Kokoro voice the narration and the courtesy lines use. The question
still goes up as audio — it is never transcribed in front of the model, which
is the stage Moshi's argument is actually about — and the synthesis is
streamed sentence by sentence as the words arrive, so the first sound does
not wait for the last word.

# The call goes through the shared client

An audio model answers with `content: null` and puts the words in
`audio.transcript`; `ai.client` maps that transcript onto ordinary
`TextDelta`s, so the `said` events below come from `TextDelta` and not from a
field named transcript.

# What the page hears

Server-sent events, the vocabulary the Rust route sent and the player reads:
`speaker`, `said` (with its `audio` frames: base64 PCM16 at `REPLY_RATE`),
`hold` and `hold_audio` while a slow answer is prepared, `heard` (the local
transcript of the question) or `heard_failed`, then `done` with the answer's
three parts. `silent` instead of all of it when the clip held no speech, and
`failed` with a sentence when the turn could not be answered.
"""

import asyncio
import base64
import json
import logging
import uuid
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any

import numpy as np

from opennotebook import speech
from opennotebook.ai import client, ledger
from opennotebook.ai.client import TextDelta
from opennotebook.ai.errors import AiError
from opennotebook.db.models import Session
from opennotebook.domain import sessions
from opennotebook.domain import settings as st
from opennotebook.domain.sessions import Line, Part, Speaker
from opennotebook.errors import SERVER_FAULT
from opennotebook.script import budget
from opennotebook.speech import banter, vad
from opennotebook.speech.banter import Bank
from opennotebook.speech.wav import NotWav, parse

log = logging.getLogger(__name__)

# Reply audio goes out as raw 16-bit little-endian PCM at this rate, which is
# what the page's AudioContext is handed directly. A contract with the player:
# wrong by a factor and the answer plays chipmunked or slurred, which is a bug
# that sounds like a model problem.
REPLY_RATE = 24_000
# Audio frames are cut at 200 ms, so a long line starts playing before it has
# all arrived.
CHUNK_SAMPLES = 4_800

# How much of a line has to have played before its speaker counts as the one
# talking. Below this the listener has heard nothing of it: the page cut in
# between lines, after the next one was queued but before it made a sound.
HEARD_MS = 400

# The longest clip that can be "keep going". Anything longer is a question
# and goes straight through, with no wait for the transcript.
RESUME_MAX_SPEECH_MS = 2_500
# How long a short clip waits for its transcript before being answered
# anyway. Offline STT runs at 0.42x realtime, so 2.5 s of speech is about a
# second.
RESUME_WAIT_MS = 3_000

# How long after the question is sent the first hold line may start. The page
# plays a "thanks" line over the first couple of seconds already.
HOLD_FIRST_MS = 3_500
# How many sentences of an answer are synthesised at once.
VOICING_PARALLEL = 4
# How many of the speaker's own lines are shown to the model as a sample of
# how they talk. The most recent ones the listener heard, so the answer picks
# up where their voice actually was rather than where the session started.
VOICE_SAMPLES = 4

NO_AUDIO = "No audio was recorded. Check that the microphone is allowed, then ask again."
NO_KEY = (
    "The studio has no AI key yet. Add OPENNOTEBOOK_AI_KEY to the server's environment, "
    "then try again."
)
TOO_LONG = "That question was too long to send. Ask it in under a minute, then try again."
# A question longer than this is refused before anything reads it: a minute
# at 48 kHz is under 6 MB.
MAX_UPLOAD_BYTES = 24 * 1024 * 1024


@dataclass(frozen=True)
class At:
    """Where the playhead was when the listener cut in. Carried so the answer
    can be grounded in what they had actually heard, not in the whole deck."""

    slide: int = 0
    line: str = ""
    offset_ms: int = 0


@dataclass
class Narration:
    """What the turn reads of an output: its voices and its parts in order."""

    speakers: list[Speaker]
    parts: list[Part]
    audio: bool = False

    @classmethod
    def of(cls, o: Session) -> Narration:
        return cls(
            [Speaker.of_json(v) for v in o.speakers],
            sessions.in_order([Part.of_json(v) for v in o.slides]),
            o.audio is not None,
        )

    def lines(self) -> list[Line]:
        """Every line of the session, in the order it is played."""
        return [line for p in self.parts for line in p.lines_in_order()]


# ── who answers ───────────────────────────────────────────────────────────────


def floor_index(lines: list[Line], at: At) -> int | None:
    """The line whose speaker last actually spoke.

    `at.line` is the line under the playhead, which is not always the one the
    listener last heard: a hand raised in the gap between two lines arrives
    with the NEXT line queued at offset zero. Answering from that line's
    speaker puts the question to someone who has not said a word yet, which
    is exactly what a real conversation never does. So an unheard line hands
    the floor back to the one before it."""
    i = next((k for k, line in enumerate(lines) if line.line_id == at.line), None)
    if i is None:
        return None
    return i - 1 if at.offset_ms < HEARD_MS and i > 0 else i


def floor_speaker(n: Narration, at: At) -> Speaker | None:
    """Who answers: the speaker who last held the floor.

    Before the first line starts nobody holds it, so the decided fallback
    applies: `speakers[0]`, session order, not a new field.

    One lookup for the whole turn: the hold lines and the answer are all
    synthesised from this speaker's voice id, and the answer's prompt is
    written in this speaker's persona. There used to be a second function
    mapping the speaker onto the answer model's voice pool, and it was the
    reason a turn had two people in it."""
    lines = n.lines()
    i = floor_index(lines, at)
    if i is not None:
        found = next((sp for sp in n.speakers if sp.speaker_id == lines[i].speaker_id), None)
        if found is not None:
            return found
    return n.speakers[0] if n.speakers else None


def display_name(sp: Speaker) -> str:
    # Outputs built before speakers had names still say "Host"; the answer
    # names them the way the player does.
    return sessions.display_name(sp.display_name, sp.voice_id) or sp.speaker_id


def narrating_speaker(n: Narration, at: At) -> tuple[str, str]:
    """The floor speaker's own voice and display name."""
    sp = floor_speaker(n, at)
    return ("", "Studio") if sp is None else (sp.voice_id, display_name(sp))


# ── what the model is told ────────────────────────────────────────────────────


def context_for(n: Narration, at: At, length: str, language: str) -> str:
    """What the model is told, and nothing more.

    Two parts. Who is answering: the speaker who last held the floor, with
    their name, their role, who else is on the session, and a few of their
    own lines as a sample of how they talk. Without that the answer was
    written by "one of the narrators" and came out in a neutral assistant
    register, spoken in a voice that had been joking or teaching a second
    earlier, so the listener heard the same person turn into someone else.

    And what they know. Phase 2 §4: the unit is the slide, not a count of
    lines. The current slide's narration and the previous slide's, because a
    question asked early on a slide is usually about what just finished. Both
    are bounded by the script generator's own character budgets, so this
    cannot grow without limit. The narration is what was written from the
    output's sources, so the answer is grounded in them."""
    lines = n.lines()
    floor = floor_index(lines, at)
    sp = floor_speaker(n, at)

    out: list[str] = []
    if sp is not None:
        name = display_name(sp)
        out.append(
            f"You are {name}, one of the voices of a narrated slide session the listener is "
            "watching."
        )
        if sp.role.strip():
            out.append(f" Your role in it: {sp.role.strip()}.")
        others = [
            f"{display_name(o)} ({o.role.strip()})" if o.role.strip() else display_name(o)
            for o in n.speakers
            if o.speaker_id != sp.speaker_id
        ]
        if others:
            out.append(f" Also on the session: {', '.join(others)}.")
        out.append(
            " You were the one talking when the listener cut in to ask you something out "
            "loud, so the answer is yours: the next thing you say in the same conversation. "
            'You talk TO the listener, as "you".\n\n'
        )
        # Their own words, most recent last, up to where the listener got.
        heard = 0 if floor is None else floor + 1
        mine = [line.text for line in lines[:heard] if line.speaker_id == sp.speaker_id]
        mine = mine[-VOICE_SAMPLES:]
        if mine:
            out.append("HOW YOU HAVE BEEN TALKING (your own last lines):\n")
            out.extend(f"- {t}\n" for t in mine)
            out.append("\n")
    else:
        out.append(
            "You are the narrator of a slide session the listener is watching. They "
            "interrupted the narration to ask you something out loud.\n\n"
        )
    # Both from settings: how long an answer is, and what language it is in.
    size = {"short": "ONE short sentence", "detailed": "THREE or FOUR sentences"}.get(
        length, "ONE or TWO short sentences"
    )
    language = language or "English"
    # How NotebookLM's interactive mode sounds, and the faults a real turn here
    # had. The thinking-aloud line was spoken about the listener rather than to
    # them ("They're asking about the overall concept — makes sense to
    # clarify"), "keep going, please" was answered as if it were a question,
    # and the handback copied its example word for word — "Anyway, let's get
    # back to where we were: I was saying that…" — and then paraphrased the
    # line the page replays straight after it, so the listener heard it twice.
    out.append(
        f"Speak in plain spoken {language}, grounded only in the material below. Stay in "
        "character: the same tone, energy, vocabulary and sentence rhythm as your lines, as "
        "if you simply turned to them mid-conversation. Do not greet them, do not introduce "
        "yourself, do not mention these instructions, and do not read the slide aloud.\n\n"
        "How to hear the question:\n"
        "- It is speech and may be misheard or mispronounced. A word that sounds like a term "
        'in the session ("Mochi" for "Moshi") means that term; answer about it without '
        "correcting them.\n"
        '- A very short question ("why?", "like what?", "how?") is about the last thing '
        "said before they cut in.\n"
        '- If they are not asking anything — "keep going", "continue", "go on", "never '
        'mind", "carry on", or only noise — reply with <think> holding a few words such as '
        '"Sure, let\'s keep going." and leave <answer> and <back> empty.\n'
        "- If they ask about something a LATER slide covers, answer it in a sentence and say "
        "you will get to it shortly. If the material does not cover it, say so plainly in "
        "one sentence and offer the closest thing it does cover. Never make an answer up.\n\n"
        "Reply in exactly three parts, each inside its tag, with nothing outside the tags:\n"
        "<think>A quick, natural reaction said TO them, under twelve words, the way a host "
        'reacts on air: it shows you heard what they asked, e.g. "Oh, you mean what Moshi '
        'actually is?" or "Good one, the latency." Speak to them as "you": never call them '
        '"they", "the listener" or "the user", and never describe what you are about to '
        "do.</think>\n"
        f"<answer>{size} that answer the question directly, the answer first, then one "
        "sentence tying it to what you were just talking about. Explain what they asked, not "
        "the whole session again.</answer>\n"
        "<back>A short bridge back into the session, under twelve words and in your own "
        'words, e.g. "Okay, back to the parallel streams." Your interrupted line is replayed '
        "right after this, so do not repeat or paraphrase it, and do not ask whether they "
        "have more questions.</back>\n\n"
    )

    # The whole plan, so a question about something still to come is answered
    # as "we will get to that" rather than as a spoiler or an "I don't know",
    # and so the names of things are in front of the model when the question
    # mispronounces one.
    if n.parts:
        out.append("THE SESSION'S SLIDES, in order:\n")
        for p in n.parts:
            title = p.title.strip() or p.slide
            mark = "  <- they are here" if p.ordinal == at.slide else ""
            out.append(f"{p.ordinal + 1}. {title}{mark}\n")
        out.append("\n")

    here = next((p for p in n.parts if p.ordinal == at.slide), None)
    before = next((p for p in n.parts if p.ordinal == at.slide - 1), None)
    if before is not None:
        out.append(f"PREVIOUS SLIDE ({before.slide}):\n")
        out.extend(f"{line.text}\n" for line in before.lines_in_order())
        out.append("\n")
    if here is not None:
        out.append(f"CURRENT SLIDE ({here.slide}):\n")
        # Which lines were actually heard matters: an answer that refers to
        # something the listener has not reached yet is a spoiler, and one that
        # re-explains what they just heard is noise. The interrupted line is
        # the boundary, so it is marked rather than dropped. A line cut into
        # before any of it played was not heard, so the mark goes in front of
        # it.
        reached = True
        for line in here.lines_in_order():
            cut_here = line.line_id == at.line
            if cut_here and at.offset_ms < HEARD_MS:
                out.append("[the listener cut in here, before this next line]\n")
                reached = False
            if reached:
                out.append(f"{line.text}\n")
            if cut_here and reached:
                out.append(f"[the listener cut in here, {at.offset_ms} ms into this line]\n")
                reached = False
    text = "".join(out)
    # An audio overview is listened to, not watched, and its parts are
    # chapters. Same prompt otherwise: the interruption, the grounding and the
    # way back are the same.
    if n.audio:
        for a, b in (
            (
                "a narrated slide session the listener is watching",
                "an audio overview the listener is listening to",
            ),
            (
                "a slide session the listener is watching",
                "an audio overview the listener is listening to",
            ),
            (", and do not read the slide aloud", ""),
            ("a LATER slide", "a LATER chapter"),
            ("THE SESSION'S SLIDES", "THE EPISODE'S CHAPTERS"),
            ("PREVIOUS SLIDE (", "PREVIOUS CHAPTER ("),
            ("CURRENT SLIDE (", "CURRENT CHAPTER ("),
        ):
            text = text.replace(a, b)
    return text


# ── reading the answer as it streams ──────────────────────────────────────────


@dataclass
class SpokenAnswer:
    """A spoken answer, typed: what the model wrote, split into its three
    parts."""

    # Thinking aloud before answering.
    think: str = ""
    # The answer and a word of discussion.
    answer: str = ""
    # The handback to the session.
    back: str = ""

    def spoken(self) -> str:
        """Everything said, in order."""
        return " ".join(t for t in (s.strip() for s in (self.think, self.answer, self.back)) if t)


class _Part(Enum):
    BEFORE = auto()
    THINK = auto()
    ANSWER = auto()
    BACK = auto()
    # No tags at all: the whole reply is the answer.
    UNTAGGED = auto()


_OPEN = (("<think>", _Part.THINK), ("<answer>", _Part.ANSWER), ("<back>", _Part.BACK))
_CLOSE = {_Part.THINK: "</think>", _Part.ANSWER: "</answer>", _Part.BACK: "</back>"}


@dataclass
class AnswerStream:
    """Reads the model's reply as it streams and hands out speakable pieces in
    order: the thinking line whole as soon as it closes, the answer and the
    handback sentence by sentence as each sentence ends.

    Tags are matched on the accumulated text, so a tag split across two deltas
    ("<ans" + "wer>") is still found. A model that ignores the format is not a
    failure: once the text cannot become an opening tag, everything is treated
    as the answer."""

    text: str = ""
    # How much of `text` has been consumed.
    at: int = 0
    part: _Part = _Part.BEFORE
    answer: SpokenAnswer = field(default_factory=SpokenAnswer)

    def push(self, delta: str) -> list[str]:
        self.text += delta
        return self._drain(end=False)

    def finish(self) -> list[str]:
        return self._drain(end=True)

    def _drain(self, *, end: bool) -> list[str]:
        out: list[str] = []
        while True:
            rest = self.text[self.at :]
            if self.part is _Part.BEFORE:
                trimmed = rest.lstrip()
                skip = len(rest) - len(trimmed)
                opened = next(((t, p) for t, p in _OPEN if trimmed.startswith(t)), None)
                if opened is not None:
                    self.at += skip + len(opened[0])
                    self.part = opened[1]
                    continue
                # Not a tag, and cannot become one.
                if trimmed and not any(t.startswith(trimmed) for t, _ in _OPEN):
                    self.part = _Part.UNTAGGED
                    continue
                return out
            if self.part is _Part.UNTAGGED:
                if end:
                    self.part = _Part.ANSWER
                    self._emit(rest, out)
                    self.at = len(self.text)
                    return out
                cut = last_sentence_end(rest)
                if cut is not None:
                    self.answer.answer += rest[:cut]
                    out.extend(speakable_parts(rest[:cut]))
                    self.at += cut
                return out
            close = _CLOSE[self.part]
            i = rest.find(close)
            if i >= 0:
                self._emit(rest[:i], out)
                self.at += i + len(close)
                self.part = _Part.BEFORE
                continue
            if end:
                self._emit(rest, out)
                self.at = len(self.text)
                return out
            # The thinking line is spoken whole; the others by sentence,
            # holding back anything that could be the closing tag.
            if self.part is not _Part.THINK:
                safe = rest.rfind("<")
                cut = last_sentence_end(rest[: len(rest) if safe < 0 else safe])
                if cut is not None:
                    self._record(rest[:cut])
                    out.extend(speakable_parts(rest[:cut]))
                    self.at += cut
            return out

    def _emit(self, body: str, out: list[str]) -> None:
        """A finished stretch of the current part: recorded, and handed out
        whole (thinking) or by sentence (the rest)."""
        self._record(body)
        t = body.strip()
        if not t:
            return
        if self.part is _Part.THINK:
            out.append(t)
        else:
            out.extend(speakable_parts(t))

    def _record(self, text: str) -> None:
        if self.part is _Part.THINK:
            self.answer.think += text
        elif self.part is _Part.BACK:
            self.answer.back += text
        else:
            self.answer.answer += text


def last_sentence_end(text: str) -> int | None:
    """Just past the last sentence end that is followed by a space, so "2.5"
    and a sentence still being written do not count."""
    end = None
    for i, c in enumerate(text[:-1]):
        if c in ".!?" and text[i + 1].isspace():
            end = i + 1
    return end


def speakable_parts(text: str) -> list[str]:
    """An answer cut into the pieces it is synthesised in: whole sentences, so
    no sound boundary ever falls inside one."""
    out: list[str] = []
    start = 0
    for i, c in enumerate(text):
        if c in ".!?" and (i + 1 == len(text) or text[i + 1].isspace()):
            if part := text[start : i + 1].strip():
                out.append(part)
            start = i + 1
    if tail := text[start:].strip():
        out.append(tail)
    return out


MIN_CLAUSE = 48


def take_speakable(buf: str) -> tuple[str | None, str]:
    """The leading clause of `buf` and what is left of it, or None and `buf`
    whole if there is not one yet.

    The relay voices whole sentences now (`AnswerStream`); this is the rule it
    replaced, kept with its tests because they state what a boundary is.

    A boundary is a terminator followed by whitespace. The whitespace is what
    keeps `1080p.` whole and stops `gpt-4.1` being read as two sentences: mid
    token the next character is a digit or a letter, never a space. `,;:`
    count only once the clause is long enough to be worth cutting, because a
    comma in the third word buys nothing and costs a synthesis call.

    Taking the EARLIEST boundary rather than the last is deliberate: this runs
    per delta, and the first sound should leave as soon as there is something
    to say."""
    for i, c in enumerate(buf[:-1]):
        if not buf[i + 1].isspace():
            continue
        if c in ".!?" or (c in ",;:" and i >= MIN_CLAUSE):
            clause = buf[: i + 1].strip()
            return (clause or None, buf[i + 1 :])
    return None, buf


_FILLER = frozenset(
    (
        "please",
        "ok",
        "okay",
        "yes",
        "yeah",
        "yep",
        "sure",
        "just",
        "you",
        "can",
        "could",
        "um",
        "uh",
        "so",
        "alright",
        "right",
        "thanks",
        "thank",
        "fine",
        "good",
        "and",
        "oh",
        "no",
        "sorry",
        "lets",
        "let's",
        "it",
        "on",
    )
)
_RESUME = frozenset(
    (
        "keep going",
        "go",
        "go ahead",
        "continue",
        "carry",
        "resume",
        "proceed",
        "never mind",
        "nevermind",
        "nothing",
        "no question",
        "skip",
        "next",
    )
)


def is_resume(text: str) -> bool:
    """Whether what they said asks for nothing but to carry on.

    The whole utterance has to be the request, once the polite words around
    it are gone: "keep going, please" is, "keep going with the latency part"
    is a question about latency and goes to the model."""
    words: list[str] = []
    word = ""
    for c in text.lower():
        if c.isalnum() or c == "'":
            word += c
        elif word:
            words.append(word)
            word = ""
    if word:
        words.append(word)
    # "go on" and "carry on" lose their "on" with the rest of the filler.
    core = " ".join(w for w in words if w not in _FILLER)
    return bool(core) and core in _RESUME


# ── voicing ───────────────────────────────────────────────────────────────────


def pcm16_chunks(wav: bytes) -> list[bytes]:
    """A Kokoro WAV as the little-endian PCM16 frames the page's sink plays.
    Chunked so a long line starts playing before it has all arrived."""
    try:
        pcm = parse(wav)
    except NotWav as e:
        log.warning("voice: a synthesised line is not a WAV: %s", e)
        return []
    # The page builds its buffers at REPLY_RATE and nothing resamples on the
    # way. A voice server that changed rate would play back at the wrong
    # speed, which sounds like a bug in the voice rather than in the wiring.
    if pcm.rate != REPLY_RATE or pcm.channels != 1:
        log.warning(
            "voice: %s Hz x %s from the voice server, the page plays %s Hz mono",
            pcm.rate,
            pcm.channels,
            REPLY_RATE,
        )
        return []
    data = np.asarray(pcm.samples, dtype="<i2").tobytes()
    step = CHUNK_SAMPLES * 2
    return [data[i : i + step] for i in range(0, len(data), step)]


async def speak_chunk(voice: str, text: str) -> list[bytes]:
    """One sentence, spoken by the narrator, as the PCM frames the page plays.

    Not `banter.spoken`: that caches by text hash, which is right for a few
    dozen fixed courtesy lines and wrong for an answer, since no two answers
    are the same and the cache would grow without anything ever reading it
    back.

    A synthesis that fails costs the sound of that sentence and not the turn
    — the words are already on screen, which is the same bargain the courtesy
    lines make when there is no voice server."""
    # Kokoro reads a dash as nothing, so "in simpler terms—let's break it
    # down" came out as one breath. The voice gets the punctuation that makes
    # it pause (`budget.speakable`, the same rule narration uses); the words
    # on screen keep the dash as written.
    try:
        wav = await speech.speech().synthesize(budget.speakable(text), voice)
    except speech.SpeechError as e:
        log.info("voice: a sentence was not voiced: %s", e)
        return []
    return pcm16_chunks(wav)


async def spoken_line(voice: str, line: str) -> list[bytes]:
    """A courtesy line's frames, from the cache when it has been said before
    in this voice."""
    wav = await banter.spoken(voice, line)
    return [] if wav is None else pcm16_chunks(wav)


# ── the turn ──────────────────────────────────────────────────────────────────

Frame = tuple[str, str]


def said(event: str, **data: str) -> Frame:
    return event, json.dumps(data)


def sounds(event: str, chunks: list[bytes]) -> list[Frame]:
    return [(event, base64.b64encode(c).decode()) for c in chunks]


def failed(sentence: str) -> Frame:
    """A failure the listener can see, on the same channel as a success."""
    return said("failed", t=sentence)


def silent(a: vad.Analysis) -> Frame:
    """The clip held no speech at all.

    Phase 2 §8: "a listener who pressed the key by accident should not be
    told off", so this is deliberately NOT a `failed` frame. The page takes
    the turn's bubbles back and resumes narration, and nothing on screen
    claims a question was asked. The numbers ride along because a gate that
    drops a turn without saying why is the silent-empty pattern wearing a new
    hat."""
    return "silent", json.dumps(
        {"input_ms": a.input_ms, "speech_ms": a.speech_ms, "segment_count": a.segment_count}
    )


@dataclass(frozen=True)
class Turn:
    """Everything one question needs, read before the stream starts so the
    stream holds no database session."""

    owner_id: uuid.UUID
    collection_id: uuid.UUID
    session_id: uuid.UUID
    wav: bytes
    speech_ms: int
    model: str
    system: str
    speaker: str
    voice: str
    # Courtesy lines on and the language English: hold lines may fill a wait.
    hold: bool


def prepare(o: Session, values: dict[str, str], at: At, wav: bytes, speech_ms: int) -> Turn:
    n = Narration.of(o)
    voice, speaker = narrating_speaker(n, at)
    language = values.get(st.LANGUAGE_KEY, "")
    # Hold lines are English, like the rest of the courtesy bank, and follow
    # the courtesy switch: off, or a session in another language, and the wait
    # is silent rather than bilingual.
    hold = st.is_on(values.get(st.COURTESY_KEY, "on")) and language in ("", "English")
    return Turn(
        owner_id=o.owner_id,
        collection_id=o.collection_id,
        session_id=o.id,
        wav=wav,
        speech_ms=speech_ms,
        model=values.get(st.ANSWER_MODEL_KEY) or st.ANSWER_MODEL_DEFAULT,
        system=context_for(n, at, values.get(st.ANSWER_LENGTH_KEY, ""), language),
        speaker=speaker,
        voice=voice,
        hold=hold,
    )


class _Failure:
    def __init__(self, sentence: str) -> None:
        self.sentence = sentence


async def turn(t: Turn) -> AsyncIterator[Frame]:
    """The answer to one spoken question, as the frames the page reads.

    Two things run side by side and their frames are merged: the relay (the
    model's answer, voiced), and the question's local transcript, which lands
    when it lands. The stream ends once both have."""
    out: asyncio.Queue[Frame | None] = asyncio.Queue()
    # The question is transcribed LOCALLY and in parallel, never in front of
    # the answer. Phase 2 §8 made showing it a requirement: measured word
    # error rate on this project's own vocabulary is 6% and every error was
    # domain jargon, so a misheard question otherwise produces a fluent answer
    # to something nobody asked with nothing on screen to show it. Offline STT
    # runs at 0.42x realtime, so a five second question costs about two
    # seconds — which would double time-to-first-sound if it were a step. It
    # is not a step. One transcription, read twice: by the `heard` frame, and
    # — for a clip short enough to be "keep going" — by the relay before it
    # speaks.
    transcript = asyncio.create_task(speech.speech().transcribe(t.wav))
    tasks: list[asyncio.Task[Any]] = [transcript]

    def start(work: Callable[[], Coroutine[Any, Any, None]]) -> None:
        async def run() -> None:
            try:
                await work()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("voice turn for %s", t.session_id)
                out.put_nowait(failed(SERVER_FAULT))
            finally:
                out.put_nowait(None)

        tasks.append(asyncio.create_task(run()))

    async def heard() -> None:
        try:
            text = await asyncio.shield(transcript)
        except speech.SpeechError as e:
            out.put_nowait(said("heard_failed", t=e.sentence))
            return
        # An empty transcript is NOT the discriminator for "said nothing":
        # measured, silence, noise and unresolvable speech all come back as
        # `{"text":""}`. The gate already ran on the audio itself, so nothing
        # is claimed here rather than guessing which of the three it was.
        if text.strip():
            out.put_nowait(said("heard", t=text))

    async def answer() -> None:
        # Every model call of the turn is charged to the output's owner.
        async with ledger.spending(
            t.owner_id, "voice", collection_id=t.collection_id, session_id=t.session_id
        ):
            await _relay(t, transcript, out.put_nowait, tasks)

    start(answer)
    start(heard)
    try:
        running = 2
        while running:
            f = await out.get()
            if f is None:
                running -= 1
            else:
                yield f
    finally:
        # The listener hung up, or talked over the answer: nothing of this
        # turn goes on working for nobody.
        for task in tasks:
            task.cancel()


async def _relay(
    t: Turn,
    transcript: asyncio.Task[str],
    put: Callable[[Frame], None],
    tasks: list[asyncio.Task[Any]],
) -> None:
    """Turn the model's text stream into ours, speaking each part as it is
    ready.

    # The shape of an answer

    The model answers in three typed parts (`SpokenAnswer`): a line of
    thinking aloud, the answer and a word of discussion, and a handback to the
    session. That is how a speaker who was interrupted actually talks — "hmm,
    so you're asking whether…", the answer, "anyway, I was saying…" — and it
    is what makes a fast start possible without the answer stuttering.

    # Why this order of work

    Speaking clause by clause as words arrived stopped mid-answer whenever the
    writing or the voicing fell behind the speaking. Preparing the whole
    answer first fixed that and made the listener wait twenty-five seconds,
    filled with canned "one moment" lines. This keeps the best of both: the
    thinking line is short, so it is voiced and playing within a couple of
    seconds, and while it plays the answer's sentences are voiced several at
    a time, in order, as the model writes them. By the time the thinking line
    ends the answer's first sentence is ready behind it.

    One ordered queue carries everything, so nothing can overlap. A canned
    hold line survives only as the fallback for a model that has said nothing
    at all after a few seconds."""
    put(said("speaker", t=t.speaker))

    # The request goes out now, before the "keep going" check below, so a
    # real question loses no time to it.
    deltas: asyncio.Queue[str | _Failure | None] = asyncio.Queue()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": t.system},
        # The question itself, as a content part rather than a transcript of
        # it. No `modalities`: asking for audio out would set a provider
        # voice, and a provider voice is the bug. Without it the same model
        # reads the question's audio and answers in text, which is the only
        # form the narrator's own voice can speak.
        {
            "role": "user",
            "content": [
                {
                    "type": "input_audio",
                    "input_audio": {"data": base64.b64encode(t.wav).decode(), "format": "wav"},
                }
            ],
        },
    ]

    async def pump() -> None:
        try:
            async for ev in client.ai().stream(t.model, messages):
                if isinstance(ev, TextDelta):
                    deltas.put_nowait(ev.text)
            deltas.put_nowait(None)
        except AiError as e:
            deltas.put_nowait(_Failure(e.sentence))
        except Exception:
            log.exception("voice: the answer model's stream broke")
            deltas.put_nowait(_Failure(SERVER_FAULT))

    model = asyncio.create_task(pump())
    tasks.append(model)

    # "Keep going" is not a question. Told so in its prompt, the answer model
    # still answered it — with a "third issue" the material never mentions. A
    # short clip waits for its local transcript (about a second, under the
    # "thanks" line the page is playing anyway); when all it says is carry on,
    # the narrator says so and the session resumes, and the model's reply is
    # dropped unread.
    if t.speech_ms <= RESUME_MAX_SPEECH_MS:
        try:
            text: str | None = await asyncio.wait_for(
                asyncio.shield(transcript), RESUME_WAIT_MS / 1000
            )
        except TimeoutError, speech.SpeechError:
            text = None
        if text is not None and is_resume(text):
            model.cancel()
            line = banter.next_line(str(t.session_id), Bank.RESUME)
            put(said("said", t=f"{line} "))
            for f in sounds("audio", await spoken_line(t.voice, line)):
                put(f)
            put(said("done", t=line, think=line, answer="", back=""))
            return

    # Chosen and voiced now, while the model thinks: the first use of a hold
    # line otherwise pays its synthesis in the middle of the wait it was meant
    # to fill — measured, the first hold of a fresh voice landed twelve
    # seconds in. Cached on disk after that, for every output in that voice.
    hold_line = banter.next_line(str(t.session_id), Bank.HOLD) if t.hold else None
    hold_sound = (
        asyncio.create_task(spoken_line(t.voice, hold_line)) if hold_line is not None else None
    )
    if hold_sound is not None:
        tasks.append(hold_sound)

    parser = AnswerStream()
    # Parts to voice, in speaking order, each already being synthesised;
    # several at once, bounded, and played strictly in this order.
    order: asyncio.Queue[tuple[str, asyncio.Task[list[bytes]]] | None] = asyncio.Queue()
    voicing = asyncio.Semaphore(VOICING_PARALLEL)
    spoken_any = False

    async def voiced(text: str) -> list[bytes]:
        async with voicing:
            return await speak_chunk(t.voice, text)

    def queue(parts: list[str]) -> None:
        for p in parts:
            task = asyncio.create_task(voiced(p))
            tasks.append(task)
            order.put_nowait((p, task))

    async def read() -> str | None:
        """The model's text into parts; a failure's sentence, or None."""
        while True:
            d = await deltas.get()
            if d is None:
                queue(parser.finish())
                order.put_nowait(None)
                return None
            if isinstance(d, _Failure):
                return d.sentence
            queue(parser.push(d))

    async def speak() -> None:
        nonlocal spoken_any
        while (item := await order.get()) is not None:
            text, sound = item
            audio = await sound
            spoken_any = True
            # The words go out with their sound, so the bubble shows what is
            # being said, not what is coming.
            put(said("said", t=f"{text} "))
            for f in sounds("audio", audio):
                put(f)

    async def hold() -> None:
        if hold_line is None or hold_sound is None:
            return
        await asyncio.sleep(HOLD_FIRST_MS / 1000)
        if spoken_any:
            return
        audio = await hold_sound
        # Re-checked: the answer may have started while the line was voiced,
        # and a hold line after the answer has begun is a stutter.
        if spoken_any:
            return
        put(said("hold", t=hold_line))
        for f in sounds("hold_audio", audio):
            put(f)

    speaker = asyncio.create_task(speak())
    holder = asyncio.create_task(hold())
    tasks.extend((speaker, holder))
    reader = asyncio.create_task(read())
    tasks.append(reader)
    why = await reader
    if why is not None:
        speaker.cancel()
        holder.cancel()
        put(failed(why))
        return
    await speaker
    holder.cancel()
    a = parser.answer
    put(said("done", t=a.spoken(), think=a.think, answer=a.answer, back=a.back))
