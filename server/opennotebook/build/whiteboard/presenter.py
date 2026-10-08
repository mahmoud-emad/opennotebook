"""The narration of a whiteboard video, as one person would say it while
drawing: rewritten for the ear, then voiced a paragraph at a time.

A session's script is written as lines for a deck or a conversation, and
voiced line by line. Over a whiteboard that sounds like a machine reading
captions: each line starts its intonation afresh after a pause, and a line
that is half a sentence breaks the sentence in two. So a whiteboard gets
its own narration (docs/video-overview-spec.md, phase 4):

* **Written to be spoken.** One call turns the script into what a good
  teacher would say to a curious friend: complete sentences, contractions,
  short ones mostly, a signpost where the idea turns. Every fact, number and
  name is kept and none is added; the rewrite is checked for that, and one
  that fails is asked again, then replaced by the script's own words joined
  into whole sentences.
* **Opened and closed as a video is.** The script's own opening (the hook)
  and closing (the takeaway) are moved out of its first and last parts into
  an opening and a closing of their own: the hook and a breath on what the
  video covers, and what to remember with a short thanks. Each is a
  chapter, "Introduction" and "Recap", shown on a fixed slide (`frame.py`).
  With them come what the slides write: a sentence on what the video is
  about, each part's title in a few words, and the takeaways.
* **Voiced by the paragraph.** Each paragraph of two or three sentences is
  one clip, so the voice carries its intonation across sentences as a person
  does. Word timings come with it from the speech server, or are estimated.

The session's own narration, deck and audio are untouched: the video's
voice is its own files, under `video/<sid>/voice/`.
"""

import logging
import re
from dataclasses import dataclass, field

from opennotebook import speech, storage
from opennotebook.ai import client
from opennotebook.build import wav
from opennotebook.build.errors import EmptyAudio, FilesNotWritten, Voice
from opennotebook.build.whiteboard import ground
from opennotebook.build.whiteboard.reply import json_of
from opennotebook.domain.sessions import Line, Part
from opennotebook.script.budget import speakable

log = logging.getLogger(__name__)

PRESENTER_MARK = "You are the teacher who will say this lesson on camera"

# The chapter titles of the opening and the closing, as their slides head
# them (`frame.py`).
OPENING = "Introduction"
CLOSING = "Recap"

# What the opening and closing slides write, at most: an agenda item, the
# sentence on what the video is about, each takeaway, and how many
# takeaways.
AGENDA_CHARS = 40
ABOUT_CHARS = 140
TAKEAWAY_CHARS = 90
TAKEAWAYS = 4

RULES = f"""{PRESENTER_MARK}, drawing on a whiteboard as you
talk. You get the lesson's script, part by part. Rewrite it as what you would actually say.

How it should sound:
- Like a good teacher explaining to one curious friend: warm, clear, unhurried. Talk to them
  ("you", "we"). Use contractions.
- Complete sentences only, never a fragment. Mostly short ones, with a longer one now and then.
- Signpost where the idea turns ("So here's the thing.", "Now,", "Notice that"), sparingly.
- No hype and no stock phrases: never "dive in", "unpack", "buckle up", "in today's video",
  "let's explore", "fascinating", "game-changer".
- Do not read lists out; say how the things connect.

What it must keep:
- Every fact, number and name in the script, exactly. Add no new fact, number, name or example.
  You may drop repetition.
- The parts, in order. Each part about as long as the script's part.
- Each part as paragraphs of one to three sentences: a paragraph is said in one breath of
  thought, about 8 to 15 seconds.

Open and close it as a video:
- "opening": what you say before part 0. The script's own opening (its hook, at the start of
  part 0) goes here, not in part 0; then one sentence on what the video covers, naming the
  parts in order in a natural way. Two to four sentences.
- "closing": what you say after the last part. The script's own closing (the takeaway at the
  end of the last part) goes here, not in the part; then a short, warm thanks for watching.
  Two or three sentences. Nothing new.

What the opening and closing slides write (short, plain, no new facts):
- "about": one sentence on what the video is about, at most {ABOUT_CHARS} characters.
- "agenda": each part's title as the opening slide lists it, at most {AGENDA_CHARS}
  characters, in the parts' order.
- "takeaways": the two to four things to remember, each at most {TAKEAWAY_CHARS} characters.

Answer with JSON only, no prose and no code fence:
{{"opening": ["..."], "parts": [{{"ordinal": 0, "paragraphs": ["...", "..."]}}],
"closing": ["..."], "about": "...", "agenda": ["...", "..."], "takeaways": ["...", "..."]}}"""

# A part's rewrite may be this much shorter or longer than its script, in
# words, before it is taken to have dropped or invented something.
LENGTH_RANGE = (0.6, 1.6)
# A paragraph, one clip, is at most this many sentences and words: one
# breath of thought, about 15 seconds said.
PARAGRAPH_SENTENCES = 3
PARAGRAPH_WORDS = 45
# The most the rewrite may be answered with: the whole narration, as JSON.
MAX_TOKENS = 8_000
# The rewrite is asked for once more when it has problems; then the
# script's own words stand in.
TRIES = 2

SENTENCE = re.compile(r"(?<=[.!?])\s+")
# A capitalised word that does not start a sentence: a name.
NAME = re.compile(r"(?<![.!?]\s)(?<!^)\b([A-Z][a-zA-Z]+)\b")


@dataclass
class Narration:
    """The video's narration, as paragraphs: its opening, each part, its
    closing; the agenda, one short title per part; and whether the rewrite
    was used."""

    opening: list[str]
    parts: list[list[str]]
    closing: list[str]
    agenda: list[str]
    about: str = ""
    takeaways: list[str] = field(default_factory=list[str])
    rewritten: bool = True


def short(title: str, chars: int = AGENDA_CHARS) -> str:
    """A title cut to whole words within `chars`."""
    out = ""
    for w in title.split():
        if len(f"{out} {w}".strip()) > chars:
            break
        out = f"{out} {w}".strip()
    return out or title[:chars]


def _words(text: str) -> int:
    return len(text.split())


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE.split(text.strip()) if s.strip()]


def joined(lines: list[str]) -> str:
    """A part's lines as running text: a line that does not end a sentence
    runs on into the next, as it was meant to be said."""
    out = ""
    for ln in (x.strip() for x in lines):
        if not ln:
            continue
        if not out:
            out = ln
        elif out[-1] in ".!?":
            out += " " + ln[0].upper() + ln[1:]
        elif ln[0].islower():
            out += ", " + ln
        else:
            out += ". " + ln
    if out and out[-1] not in ".!?":
        out += "."
    return out


def paragraphs(text: str) -> list[str]:
    """Running text cut into paragraphs of whole sentences, each up to
    `PARAGRAPH_SENTENCES` sentences or `PARAGRAPH_WORDS` words."""
    out: list[str] = []
    cur: list[str] = []
    for s in _sentences(text):
        if cur and (
            len(cur) >= PARAGRAPH_SENTENCES or _words(" ".join([*cur, s])) > PARAGRAPH_WORDS
        ):
            out.append(" ".join(cur))
            cur = []
        cur.append(s)
    if cur:
        out.append(" ".join(cur))
    return out


def plain(parts: list[Part]) -> Narration:
    """The script's own words, joined into whole sentences and paragraphs:
    what stands in when the rewrite cannot be used. The script opens and
    closes its first and last parts, so their first and last paragraphs are
    the opening and the closing, where a part keeps one besides."""
    paras = [paragraphs(joined([ln.text for ln in p.lines_in_order()])) for p in parts]
    opening: list[str] = []
    closing: list[str] = []
    if paras and len(paras[0]) > 1:
        opening = [paras[0].pop(0)]
    if paras and len(paras[-1]) > 1:
        closing = [paras[-1].pop()]
    return Narration(opening, paras, closing, [short(p.title) for p in parts], rewritten=False)


def problems(
    parts: list[Part], rewrite: list[list[str]], sources: list[str],
    opening: list[str] | None = None, closing: list[str] | None = None,
    slides: list[str] | None = None,
) -> list[str]:  # fmt: skip
    """What is wrong with a rewrite: a part missing or changed in length
    beyond `LENGTH_RANGE`, or a number or a name the script and its sources
    do not have. The opening is measured with the first part and the closing
    with the last, as they were taken from them."""
    if len(rewrite) != len(parts):
        return [f"there are {len(rewrite)} parts, not {len(parts)}"]
    original = [" ".join(ln.text for ln in p.lines) for p in parts]
    said = ground.Said.of([*original, *(p.title for p in parts), *sources])
    found: list[str] = []
    for frame, what in ((opening, "the opening"), (closing, "the closing"), (slides, "the slides")):
        found += _invented(what, " ".join(frame or []), said)
    lo, hi = LENGTH_RANGE
    for i, (p, paras) in enumerate(zip(original, rewrite, strict=True)):
        text = " ".join(paras)
        if not paras or not text.strip():
            found.append(f"part {i} is empty")
            continue
        before = (opening or []) if i == 0 else []
        after = (closing or []) if i == len(parts) - 1 else []
        ratio = _words(" ".join([*before, text, *after])) / max(_words(p), 1)
        if not lo <= ratio <= hi:
            found.append(
                f"part {i} is {ratio:.1f} times the script's length; keep it about the same"
            )
        found += _invented(f"part {i}", text, said)
    return found


def _invented(what: str, text: str, said: ground.Said) -> list[str]:
    """A problem when `text` has a number or a name that was not said."""
    new = [n for n in ground.NUMBER.findall(text) if said.unsaid(n)]
    names = [n for n in NAME.findall(text) if said.unsaid(n)]
    if not (new or names):
        return []
    listed = ", ".join(repr(x) for x in [*new, *names])
    return [f"{what} says {listed}, which the script does not: keep only its facts"]


def _texts(v: object) -> list[str]:
    items: list[object] = v if isinstance(v, list) else []
    return [str(x).strip() for x in items if str(x).strip()]


def _parse(text: str, parts: list[Part]) -> Narration:
    """The rewrite as a `Narration`: its texts cut into paragraphs, and what
    the slides write held to their lengths. `ValueError`, `KeyError` or
    `TypeError` when it is not the JSON asked for."""
    v = json_of(text)
    got = sorted(v["parts"], key=lambda p: int(p.get("ordinal", 0)))
    agenda = [short(a) for a in _texts(v.get("agenda"))]
    if len(agenda) != len(parts):
        agenda = [short(p.title) for p in parts]
    about = str(v.get("about") or "").strip()
    return Narration(
        opening=paragraphs(" ".join(_texts(v.get("opening")))),
        parts=[paragraphs(" ".join(ps)) or ps for ps in (_texts(p["paragraphs"]) for p in got)],
        closing=paragraphs(" ".join(_texts(v.get("closing")))),
        agenda=agenda,
        about=about if len(about) <= ABOUT_CHARS else "",
        takeaways=[t for t in _texts(v.get("takeaways")) if len(t) <= TAKEAWAY_CHARS][:TAKEAWAYS],
    )


async def rewrite(model: str, parts: list[Part], sources: list[str]) -> Narration:
    """The narration rewritten to be spoken, opened and closed as a video,
    as paragraphs; the script's own (`plain`) when the rewrite cannot be
    used. An `AiError` is raised, not stood in for."""
    script = "\n\n".join(
        f"## Part {i}: {p.title}\n" + "\n".join(ln.text for ln in p.lines_in_order())
        for i, p in enumerate(parts)
    )
    ask = script
    for _ in range(TRIES):
        try:
            done = await client.ai().complete(
                model,
                [{"role": "system", "content": RULES}, {"role": "user", "content": ask}],
                max_tokens=MAX_TOKENS,
            )
            got = _parse(done.text, parts)
        except (ValueError, KeyError, TypeError) as e:
            found = [f"the answer could not be read: {e}"]
        else:
            found = problems(
                parts, got.parts, sources, got.opening, got.closing, [got.about, *got.takeaways]
            )
            if not got.opening or not got.closing:
                found.append("the opening and the closing must each have at least one sentence")
            if not found:
                return got
        log.info("the spoken narration needs another try: %s", "; ".join(found))
        fixes = "\n- ".join(found)
        ask = f"{script}\n\nYour last answer had these problems; fix every one:\n- {fixes}"
    return plain(parts)


def voice_dir(sid: object) -> str:
    """Where the video's own voice is kept, apart from the session's audio."""
    return f"video/{sid}/voice"


@dataclass
class Voiced:
    """The video's parts as voiced, its opening and closing among them as
    parts of their own, and which those are (by ordinal; None when absent)."""

    parts: list[Part]
    opening: int | None
    closing: int | None
    agenda: list[str]
    about: str
    takeaways: list[str]
    rewritten: bool


async def record(
    voice_client: speech.Speech, sid: object, voice: str, speaker: str,
    parts: list[Part], spoken: Narration,
) -> Voiced:  # fmt: skip
    """The video's parts, one line per paragraph, each voiced in one go with
    its word timings: the opening first and the closing last, each a part of
    its own, the others numbered after the opening."""
    first, last = parts[0], parts[-1]
    plan: list[tuple[Part, str, list[str]]] = []
    if spoken.opening:
        plan.append((first, OPENING, spoken.opening))
    plan += [(p, p.title, paras) for p, paras in zip(parts, spoken.parts, strict=True)]
    if spoken.closing:
        plan.append((last, CLOSING, spoken.closing))
    out: list[Part] = []
    for ordinal, (p, title, paras) in enumerate(plan):
        lines: list[Line] = []
        for k, text in enumerate(paras):
            line = Line(f"v{ordinal}p{k}", speaker, k, text)
            try:
                data, words = await voice_client.synthesize_timed(speakable(text), voice)
            except speech.SpeechError as e:
                raise Voice(str(e), e.sentence) from e
            if not data:
                raise EmptyAudio(line.line_id)
            try:
                line.audio_path = storage.put(f"{voice_dir(sid)}/{line.line_id}.wav", data)
            except OSError as e:
                raise FilesNotWritten(f"line {line.line_id}: {e}") from e
            line.duration_ms = wav.duration_of(data, line.line_id)
            line.cues = [w.as_cue() for w in words]
            lines.append(line)
        out.append(Part(p.slide, ordinal, title, p.on_slide, lines, p.collection, p.presentation))
    return Voiced(
        out,
        opening=0 if spoken.opening else None,
        closing=len(out) - 1 if spoken.closing else None,
        agenda=spoken.agenda,
        about=spoken.about,
        takeaways=spoken.takeaways,
        rewritten=spoken.rewritten,
    )
