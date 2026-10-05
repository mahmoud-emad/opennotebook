"""Parsing the model's script output. A port of
`opennotebook_script/src/parse.rs`.

The wire format is one line per spoken line, `speaker_id: text`. Chosen over
JSON because a malformed line is one bad line rather than an unparseable
document, and because a spoken line has no structure worth nesting.
"""

from dataclasses import dataclass

from opennotebook.script.errors import ScriptError


@dataclass(frozen=True)
class ScriptedLine:
    speaker_id: str
    text: str


class UnknownSpeaker(ScriptError):
    """A line tagged with somebody who is not a speaker of this session."""

    def __init__(self, line: int, speaker: str) -> None:
        super().__init__(f"line {line}: `{speaker}` is not a speaker of this session")
        self.line = line
        self.speaker = speaker

    @property
    def sentence(self) -> str:
        return (
            "The AI model wrote lines for a speaker this output does not have. Try again; if it "
            "keeps happening, pick another script model in Settings › Models."
        )


def _norm(s: str) -> str:
    return "".join(c.lower() for c in s if c.isascii() and c.isalnum())


def resolve(tag: str, known: list[str]) -> str | None:
    """Which declared speaker a tag names, if any.

    Exact first. Then the same id under punctuation and capitals, then the
    same id with the word "id" stuck on the end — `**Host**` and `host_id`
    are both `host`. That last one is not hypothetical: with one speaker
    called `host`, `amazon/nova-micro-v1` read the format line "lines of the
    form `speaker_id: what they say`" and tagged every line `host_id`, which
    failed a prep five minutes in over a name the model had got right.

    What it deliberately does NOT do is guess. A tag naming somebody who is
    not in this session — `narrator` where the speakers are `host` and
    `expert` — is still an error, because that is a line whose speaker is
    genuinely unknown.
    """
    if tag in known:
        return tag
    t = _norm(tag)
    if not t:
        return None
    stripped = t.removesuffix("id") if t.endswith("id") and len(t) > 2 else None
    for k in known:
        n = _norm(k)
        if n and n in (t, stripped):
            return k
    # A label that names a speaker by POSITION rather than by id.
    #
    # The prompt shows the real ids and asks for them, and the script model is
    # deliberately small, so it substitutes its own conventions: `A:`/`B:`
    # cost a real prep with "line 3: `A` is not a speaker of this session".
    # `1:`, `Speaker 1:` and `S1:` are the same idea wearing different
    # clothes. The intent is unambiguous — first speaker, second speaker — so
    # it is honoured rather than refused. A tag that names nobody still is an
    # error.
    if t.startswith("speaker"):
        ordinal = t[len("speaker") :]
    elif t.startswith("s"):
        ordinal = t[1:]
    else:
        ordinal = t
    if ordinal.isdigit() and int(ordinal) >= 1:
        n = int(ordinal)
        return known[n - 1] if n <= len(known) else None
    # A single letter: a, b, c … taken as first, second, third.
    if len(t) == 1 and "a" <= t <= "z":
        i = ord(t) - ord("a")
        return known[i] if i < len(known) else None
    return None


def parse_lines(raw: str, known: list[str]) -> list[ScriptedLine]:
    """Parse `speaker: text` lines, keeping only speakers the session
    declares.

    An unknown speaker is an error rather than a dropped line. Dropping would
    leave a session whose narration silently lost a turn.

    A tag that is a known id wearing punctuation, capitals or the word "id"
    is resolved rather than refused — see `resolve`. That is not leniency
    about who is speaking: it is the same speaker, spelled the way a small
    model spells things.
    """
    out: list[ScriptedLine] = []
    for n, raw_line in enumerate(raw.splitlines()):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        speaker, sep, said = line.partition(":")
        if not sep:
            continue
        speaker = speaker.strip().lstrip("-* ")
        said = said.strip()
        if not said:
            continue
        found = resolve(speaker, known)
        if found is None:
            raise UnknownSpeaker(n + 1, speaker)
        out.append(ScriptedLine(found, said))
    return out


# The allowed `Layout:` values, kept verbatim from the slide spec of an
# earlier slide service. A layout it does not know is not a layout: the
# renderer picks the composition from this closed set, so an invented name
# degrades to whatever it guesses. Validated here rather than trusted,
# because the model supplies it.
LAYOUTS = (
    "centered title",
    "left text / right image",
    "right text / left image",
    "two columns",
    "full-bleed image with overlay text",
    "stat + supporting text",
    "quote centered",
    "text only",
)

# The element type labels a slide line may carry, plus the image slot.
ELEMENT_LABELS = ("Subhead", "Point", "Stat", "Quote", "Caption")


def _is_layout_line(t: str) -> bool:
    """Whether `t` is a `Layout:` line. Dropped from the spoken half
    wherever it appears: `layout_of` reads it off the whole reply, so nothing
    is lost, and leaving it in would fail the slide as a line by a speaker
    called `Layout`."""
    key, sep, _ = t.strip("-*# ").partition(":")
    return bool(sep) and key.strip().lower() == "layout"


def _marker_rest(t: str) -> str | None:
    """The rest of the line when `t` is the slide marker, else None.

    Written to accept what models actually emit rather than what the prompt
    asks for: `SLIDE:`, `slide`, `**SLIDE:**`, `## SLIDE`, `- SLIDE:`,
    `ON SLIDE:`, and the marker with its first element trailing behind it.
    Anything looser would swallow narration, so the word itself must be the
    whole key.
    """
    trimmed = t.strip("-*#_ \t")
    key, sep, rest = trimmed.partition(":")
    rest = rest.strip() if sep else ""
    key = key.strip("*_# ").strip().lower()
    return rest if key in ("slide", "on slide") else None


def _element_line(t: str) -> str | None:
    """One element line, normalised, or None when it is not one.

    Leading bullets are stripped because the spec's own examples carry them
    and a model copies what it sees; the label is matched case-insensitively
    and re-emitted in the spec's own casing.
    """
    body = t.lstrip("-* ").strip()
    if body.startswith("[image:"):
        return body
    label, sep, rest = body.partition(":")
    if not sep:
        return None
    label, rest = label.strip(), rest.strip()
    if not rest:
        return None
    canonical = next((lb for lb in ELEMENT_LABELS if lb.lower() == label.lower()), None)
    return None if canonical is None else f"{canonical}: {rest}"


def split_slide_reply(raw: str) -> tuple[str, list[str]]:
    """Split a slide reply into its spoken half and its on-slide half.

    The model is asked for two blocks in one call: narration lines, then a
    `SLIDE:` marker, then the slide's own copy. One call, because the two are
    written from the same retrieved material and a second call would double
    the per-slide cost to say the same thing twice.

    Returns `(spoken, on_slide)`. Everything before the marker is spoken; the
    element lines after it are kept only if they are shaped like elements,
    so a model that ignores the format costs the slide's copy and not its
    narration.
    """
    spoken: list[str] = []
    elements: list[str] = []
    in_slide = False
    for line in raw.splitlines():
        t = line.strip()
        if not in_slide:
            rest = _marker_rest(t)
            if rest is not None:
                in_slide = True
                # `SLIDE: Layout: text only` on one line. The remainder is the
                # first element rather than something to drop, and the marker
                # must still be consumed — left in the spoken half it parses
                # as a line by a speaker called `SLIDE`.
                if rest and (e := _element_line(rest)):
                    elements.append(e)
                continue
            # Slide copy that arrived before the marker, or with no marker at
            # all, is routed to the elements: `parse_lines` treats an unknown
            # prefix as a fatal unknown speaker, so one stray `Point:` would
            # fail the slide. These labels cannot collide with a speaker id:
            # ids come from the session roster and are matched against it.
            if e := _element_line(t):
                elements.append(e)
                continue
            if _is_layout_line(t):
                continue
            spoken.append(line + "\n")
            continue
        if not t:
            continue
        if e := _element_line(t):
            elements.append(e)
    return "".join(spoken), elements


def layout_of(raw: str) -> str | None:
    """The layout the model asked for, or None when it named one that is not
    real."""
    for line in raw.splitlines():
        t = line.strip().lstrip("-* ").strip()
        key, sep, value = t.partition(":")
        if not sep or key.strip().lower() != "layout":
            continue
        want = value.strip().lower()
        if want in LAYOUTS:
            return want
    return None
