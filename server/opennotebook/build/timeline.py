"""When everything in an output is said: each line, each word and each
chapter, in ms from the start of the episode.

One timeline, from the same plan that joins the episode WAV
(`wav.episode_plan`): the same pauses, in the same places, after the same
measured durations. A video drawn on it and the WAV a person downloads
cannot drift apart, because neither has a clock of its own.

A word's time is measured when the speech server gave one (`Line.cues`,
from Kokoro-FastAPI) and estimated otherwise. The estimate shares the
line's spoken span out by syllables, with a beat after punctuation. Measured
against Kokoro's own timings (30 clips, 3 voices, 381 words, 2026-10-05),
it put a word's start a mean 135 ms off and 268 ms at p90: good enough
for a caption, not for drawing on the word, which is why a measured time
always wins.
"""

import html
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass

from opennotebook.build import wav
from opennotebook.domain.sessions import Line, Part, in_order

# How much of a syllable's time the voice spends on a pause after each mark.
# Fitted on the measurements above: commas and dashes are a short breath,
# a full stop a longer one.
PAUSE_AFTER = {",": 0.6, ";": 0.8, ":": 0.8, "—": 0.6, "–": 0.6, ".": 1.0, "!": 1.0, "?": 1.0}

# Captions: at most this many characters on screen at once, the usual
# broadcast line length.
CAPTION_CHARS = 42

# A line counts as timed by the server when this share of its words was.
MEASURED_SHARE = 0.9

VOWELS = re.compile(r"[aeiouy]+", re.IGNORECASE)
LETTERS = re.compile(r"\w", re.UNICODE)


@dataclass(frozen=True)
class Word:
    """A word as it appears in the line's text, and when it is said."""

    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class Span:
    """One line: when it is said, by whom, and its words."""

    line_id: str
    part_ordinal: int
    speaker_id: str
    text: str
    start_ms: int
    end_ms: int
    words: tuple[Word, ...]
    # True when the words' times came from the speech server.
    measured: bool


@dataclass(frozen=True)
class Chapter:
    """A part (slide or chapter) and the time it is on screen: from the
    moment the previous part's last word ends to the moment its own does."""

    ordinal: int
    title: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class Caption:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class Timeline:
    lines: tuple[Span, ...]
    chapters: tuple[Chapter, ...]
    total_ms: int


def syllables(word: str) -> int:
    """A word's syllables, near enough: groups of vowels, one at least. A
    number is read aloud digit group by digit group, so it counts long."""
    core = "".join(c for c in word if c.isalnum())
    if not core:
        return 0
    if core.isdigit():
        return max(1, len(core))
    n = len(VOWELS.findall(core))
    if core.lower().endswith("e") and n > 1 and not core.lower().endswith("le"):
        n -= 1
    return max(1, n)


def tokens_of(text: str) -> list[str]:
    """The line's words as they are shown, each carrying any piece with no
    letter in it ("&", "—", "<") beside it: a caption shows every mark the
    narration has, while only words are timed."""
    out: list[str] = []
    lead = ""
    for t in text.split():
        if LETTERS.search(t):
            out.append(f"{lead} {t}".strip())
            lead = ""
        elif out:
            out[-1] = f"{out[-1]} {t}"
        else:
            lead = f"{lead} {t}".strip()
    if lead and out:
        out[-1] = f"{out[-1]} {lead}"
    return out


def _weight(token: str) -> float:
    pause = PAUSE_AFTER.get(token.rstrip("\"')]”’")[-1:] or "", 0.0)
    return syllables(token) + pause


def estimate(text: str, start_ms: int, end_ms: int) -> tuple[Word, ...]:
    """The words of `text` spread over [start_ms, end_ms] by their weight:
    each word's syllables, plus the pause after its punctuation, which is
    spent after the word rather than in it."""
    tokens = tokens_of(text)
    if not tokens:
        return ()
    span = max(end_ms - start_ms, 0)
    weights = [_weight(t) for t in tokens]
    total = sum(weights) or 1.0
    out: list[Word] = []
    at = float(start_ms)
    for t, w in zip(tokens, weights, strict=True):
        spoken = span * syllables(t) / total
        out.append(Word(t, round(at), round(at + spoken)))
        at += span * w / total
    return tuple(out)


def _norm(word: str) -> str:
    return "".join(c for c in word.lower() if c.isalnum())


def _cues(line: Line) -> list[tuple[str, float, float]]:
    """The line's usable cues: a word with letters in it and two real
    numbers, kept inside the clip."""
    dur = max(line.duration_ms or 0, 0)
    out: list[tuple[str, float, float]] = []
    for c in line.cues:
        w, a, b = c.get("word"), c.get("start_ms"), c.get("end_ms")
        if not isinstance(w, str) or not _norm(w):
            continue
        # A bool is an int to Python; it is not a time.
        if not all(isinstance(x, int | float) and not isinstance(x, bool) for x in (a, b)):
            continue
        a, b = float(a), float(b)  # pyright: ignore[reportArgumentType]
        if not (math.isfinite(a) and math.isfinite(b)):
            continue
        a = min(max(a, 0.0), dur)
        out.append((w, a, min(max(b, a), dur)))
    return out


def align(line: Line, start_ms: float) -> tuple[tuple[Word, ...], float]:
    """The line's words, timed by the server's cues where a cue says that
    word, and between their timed neighbours where none does, with the share
    of words the server timed.

    A word is matched to one or more consecutive cues whose letters spell it:
    "is—let's" is spoken as two words after `speakable`, and a number may be
    read as several. A cue that spells nothing of the next word is skipped,
    up to two, so one word the server timed differently does not shift every
    word after it."""
    tokens = tokens_of(line.text)
    cues = _cues(line)
    times: list[tuple[float, float] | None] = [None] * len(tokens)
    j = 0
    for i, t in enumerate(tokens):
        want = _norm(t)
        for k in range(j, min(j + 3, len(cues))):
            got, m = "", k
            while m < len(cues) and len(got) < len(want):
                got += _norm(cues[m][0])
                m += 1
                if not want.startswith(got):
                    break
            if got == want:
                times[i] = (cues[k][1], cues[m - 1][2])
                j = m
                break
    dur = float(max(line.duration_ms or 0, 0))
    out: list[Word] = []
    i = 0
    while i < len(tokens):
        known = times[i]
        if known is not None:
            out.append(Word(tokens[i], round(start_ms + known[0]), round(start_ms + known[1])))
            i += 1
            continue
        # A run of words the server did not time: spread between the end of
        # the word before and the start of the word after.
        k = i
        while k < len(tokens) and times[k] is None:
            k += 1
        lo = out[-1].end_ms - start_ms if out else 0.0
        after = times[k] if k < len(tokens) else None
        hi = after[0] if after is not None else dur
        out.extend(
            estimate(" ".join(tokens[i:k]), round(start_ms + lo), round(start_ms + max(hi, lo)))
        )
        i = k
    share = sum(x is not None for x in times) / len(tokens) if tokens else 0.0
    return tuple(out), share


def timeline(parts: list[Part], exact_ms: Mapping[str, float] | None = None) -> Timeline:
    """The output's timeline. Raises `ValueError` when a line is not voiced
    yet: a timeline of guessed lengths would put the picture out of step
    with the sound.

    `exact_ms` gives each line's length to the sample, from its WAV. Without
    it the stored `duration_ms` is used, which is rounded down to the ms: over
    an hour of short lines that drifts a few hundred ms from the joined
    audio, so a render passes the exact lengths."""
    by_id = {line.line_id: (p, line) for p in parts for line in p.lines}
    spans: list[Span] = []
    at = 0.0
    for k, (line_id, _path, gap) in enumerate(wav.episode_plan(parts)):
        part, line = by_id[line_id]
        if line.duration_ms is None:
            raise ValueError(f"line {line_id} has no measured duration yet")
        length = max(exact_ms.get(line_id, line.duration_ms) if exact_ms else line.duration_ms, 0)
        if k > 0:
            at += gap
        start, end = at, at + length
        words, share = align(line, start)
        spans.append(
            Span(
                line_id,
                part.ordinal,
                line.speaker_id,
                line.text,
                round(start),
                round(end),
                words,
                share >= MEASURED_SHARE,
            )
        )
        at = end
    total = round(at)
    # Parts in order, each holding its own lines: by position, so two parts
    # that share an ordinal still get a chapter each.
    owner = {line.line_id: idx for idx, p in enumerate(in_order(parts)) for line in p.lines}
    chapters: list[Chapter] = []
    for idx, part in enumerate(in_order(parts)):
        mine = [sp for sp in spans if owner.get(sp.line_id) == idx]
        if not mine:
            continue
        start = chapters[-1].end_ms if chapters else 0
        chapters.append(Chapter(part.ordinal, part.title, start, mine[-1].end_ms))
    if chapters:
        # The last part stays on screen to the end, and nothing is before
        # the first.
        last = chapters[-1]
        chapters[-1] = Chapter(last.ordinal, last.title, last.start_ms, total)
    return Timeline(tuple(spans), tuple(chapters), total)


def _split_long(words: tuple[Word, ...]) -> list[Word]:
    """A word longer than a caption (a URL, or a sentence in a script
    written without spaces) cut into caption-sized pieces, its time shared
    by their length."""
    out: list[Word] = []
    for w in words:
        if len(w.text) <= CAPTION_CHARS:
            out.append(w)
            continue
        n = math.ceil(len(w.text) / CAPTION_CHARS)
        size = math.ceil(len(w.text) / n)
        span = w.end_ms - w.start_ms
        for i in range(n):
            piece = w.text[i * size : (i + 1) * size]
            a = w.start_ms + span * i * size // len(w.text)
            b = w.start_ms + span * min((i + 1) * size, len(w.text)) // len(w.text)
            out.append(Word(piece, a, b))
    return out


def captions(tl: Timeline) -> list[Caption]:
    """The narration as captions: each line in pieces of at most
    `CAPTION_CHARS`, broken after punctuation where it can be, each shown
    from its first word until the next piece or the line's end. Pieces never
    overlap and never run past the end."""
    out: list[Caption] = []
    for sp in tl.lines:
        pieces: list[list[Word]] = []
        cur: list[Word] = []
        for w in _split_long(sp.words):
            if cur and len(" ".join(x.text for x in [*cur, w])) > CAPTION_CHARS:
                pieces.append(cur)
                cur = []
            cur.append(w)
            # A piece that has reached half its room and ends a clause ends
            # here, rather than carry a new clause's first words.
            if (
                w.text[-1:] in PAUSE_AFTER
                and len(" ".join(x.text for x in cur)) >= CAPTION_CHARS // 2
            ):
                pieces.append(cur)
                cur = []
        if cur:
            pieces.append(cur)
        for i, piece in enumerate(pieces):
            start = sp.start_ms if i == 0 else piece[0].start_ms
            end = pieces[i + 1][0].start_ms if i + 1 < len(pieces) else sp.end_ms
            if out:
                start = max(start, out[-1].end_ms)
            end = min(end, tl.total_ms)
            if end <= start:
                # No time of its own: its words join the piece before.
                if out:
                    prev = out[-1]
                    out[-1] = Caption(
                        prev.start_ms,
                        prev.end_ms,
                        f"{prev.text} " + " ".join(w.text for w in piece),
                    )
                continue
            out.append(Caption(start, end, " ".join(w.text for w in piece)))
    return out


def _stamp(ms: int, sep: str) -> str:
    h, rest = divmod(ms, 3_600_000)
    m, rest = divmod(rest, 60_000)
    s, ms = divmod(rest, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _one_line(text: str) -> str:
    # A cue's text is one line, and an arrow in it would read as a timing.
    return " ".join(text.split()).replace("-->", "→")


def srt(caps: list[Caption]) -> str:
    """SubRip, for the video's own caption track. ffmpeg reads a few HTML
    tags in it, so `<` and `>` are swapped for look-alikes rather than
    escaped: SubRip has no escapes."""
    return "".join(
        f"{i}\n{_stamp(c.start_ms, ',')} --> {_stamp(c.end_ms, ',')}\n"
        f"{_one_line(c.text).replace('<', '‹').replace('>', '›')}\n\n"
        for i, c in enumerate(caps, 1)
    )


def vtt(caps: list[Caption]) -> str:
    """WebVTT, for the web player, its text escaped as the format asks."""
    body = "".join(
        f"{_stamp(c.start_ms, '.')} --> {_stamp(c.end_ms, '.')}\n"
        f"{html.escape(_one_line(c.text), quote=False)}\n\n"
        for c in caps
    )
    return "WEBVTT\n\n" + body
