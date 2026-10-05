"""Collection covers: designed by a model as a small spec, drawn here.

Ported from `opennotebook_server/src/cover/`. The model never writes markup.
It reads what a collection holds (the `Digest` naming reads too) and answers
with a topic, a few terms, and one choice from each of three short lists: a
motif glyph, a palette and a layout. `validate` holds the answer to those
lists and `render` draws it. Until a collection has a designed cover, with
covers off, or when the model fails, the cover is `fallback`: the same drawing
from the cid and title, which costs nothing.

When a cover is designed and how the row is written is `domain/covers.py`,
beside the naming it follows.
"""

import json
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from opennotebook.cover import draw, motifs
from opennotebook.cover.draw import Theme

__all__ = ["CoverSpec", "Theme"]

# Bumped whenever the drawing changes, so every cached cover is redrawn.
RENDER_VERSION = 2

# How long a design may take before the collection keeps the cover it has.
DESIGN_TIMEOUT = 30.0

TOPIC_MAX_CHARS = 60
TOPIC_MAX_WORDS = 8
TERM_MAX_CHARS = 32
TERM_MAX_WORDS = 4
TERMS_MAX = 5

_MASK = (1 << 64) - 1


@dataclass(frozen=True)
class CoverSpec:
    """What a cover is drawn from; stored on the collection as JSON."""

    topic: str
    terms: list[str] = field(default_factory=list[str])
    motif: str = ""
    palette: str = ""
    layout: str = ""

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, v: Any) -> CoverSpec | None:
        """A stored spec, or None when the row holds none or not one."""
        if not isinstance(v, dict) or not isinstance(v.get("topic"), str):
            return None
        terms: Any = v.get("terms")
        return cls(
            topic=v["topic"],
            terms=[t for t in terms if isinstance(t, str)] if isinstance(terms, list) else [],
            motif=str(v.get("motif") or ""),
            palette=str(v.get("palette") or ""),
            layout=str(v.get("layout") or ""),
        )


def hash(parts: list[str]) -> int:
    """FNV-1a: stable across builds and machines, which a cover version and a
    cid's shapes must be. Not a security hash. Each part ends in a 0 byte."""
    h = 0xCBF29CE484222325
    for p in parts:
        for b in (*p.encode("utf-8"), 0):
            h = ((h ^ b) * 0x100000001B3) & _MASK
    return h


def seed(cid: str) -> int:
    """What decides a cover's shapes, palette and layout when nothing else
    does."""
    # FNV's low bits barely move between cids made a moment apart; a
    # finaliser spreads them over every palette and layout.
    z = hash(["cover", cid])
    z = ((z ^ (z >> 33)) * 0xFF51AFD7ED558CCD) & _MASK
    z = ((z ^ (z >> 33)) * 0xC4CEB9FE1A85EC53) & _MASK
    return z ^ (z >> 33)


def version(spec: CoverSpec) -> str:
    """The version of a designed cover: its spec, drawn by this renderer."""
    parts = [
        str(RENDER_VERSION),
        spec.topic,
        "\x1f".join(spec.terms),
        spec.motif,
        spec.palette,
        spec.layout,
    ]
    return f"g-{hash(parts):016x}"


def fallback_version(cid: str, title: str, source_names: list[str]) -> str:
    """The version of the cover drawn from the cid and title: everything
    `fallback` reads, the source names standing in for their titles."""
    names = "\x1f".join(source_names[:TERMS_MAX])
    return f"f-{hash([str(RENDER_VERSION), cid, title.strip(), names]):016x}"


def fallback(cid: str, title: str, source_titles: list[str]) -> CoverSpec:
    """The cover drawn from the cid and title, free and always there: accent
    and layout from the cid, the title as the topic, the first sources'
    titles as the terms."""
    s = seed(cid)
    return CoverSpec(
        topic=clean_topic(title) or "Untitled collection",
        terms=clean_terms(source_titles),
        motif=motifs.FALLBACK,
        palette=draw.ACCENTS[s % len(draw.ACCENTS)].id,
        layout=draw.LAYOUTS[(s >> 8) % len(draw.LAYOUTS)][0],
    )


def render(cid: str, spec: CoverSpec, theme: Theme) -> str:
    """The page for a cover in the viewer's theme. A stored spec is held to
    the lists again, so one written by an older build, with a motif since
    dropped or one of the first palettes, still draws."""
    spec = repaired(cid, spec)
    accent = draw.accent(spec.palette) or draw.ACCENTS[0]
    return draw.page(
        draw.Cover(
            topic=spec.topic,
            terms=tuple(spec.terms),
            motif=spec.motif,
            accent=accent,
            layout=spec.layout,
            theme=theme,
            seed=seed(cid),
        )
    )


# ── the model's answer ────────────────────────────────────────────────────────


class NotACover(ValueError):
    """Why a model's reply is not a cover."""


def validate(cid: str, raw: str) -> CoverSpec:
    """The model's reply as a cover, or `NotACover` saying why it is not one.

    Strict where an answer cannot be repaired: it must be one JSON object with
    a topic. A motif, accent or layout not on the lists is replaced, not
    refused: the topic and terms are the expensive part, and the rest has a
    sound default.
    """
    found = object_in(raw)
    if found is None:
        raise NotACover("the reply holds no JSON object")
    try:
        r: Any = json.loads(found)
    except ValueError as e:
        raise NotACover(f"not a cover: {e}") from e
    if not isinstance(r, dict):
        raise NotACover("not a cover: not an object")
    reply: dict[str, Any] = r
    topic: Any = reply.get("topic")
    terms: Any = reply.get("terms", [])
    choices: list[Any] = [reply.get(k, "") for k in ("motif", "palette", "layout")]
    if not isinstance(topic, str):
        raise NotACover("not a cover: no topic")
    if not isinstance(terms, list) or not all(isinstance(c, str) for c in choices):
        raise NotACover("not a cover: a field has the wrong type")
    clean = clean_topic(topic)
    if clean is None:
        raise NotACover("the topic is empty or a sentence")
    motif, palette, lay = (str(c).strip().lower() for c in choices)
    return repaired(
        cid,
        CoverSpec(
            topic=clean,
            terms=clean_terms(t for t in terms if isinstance(t, str)),
            motif=motif,
            palette=palette,
            layout=lay,
        ),
    )


def repaired(cid: str, spec: CoverSpec) -> CoverSpec:
    """Each choice held to its list: an unknown motif is the collection glyph,
    an unknown accent or layout the cid's own. One of the first palettes or
    layouts is read as its nearest accent or layout."""
    accent = draw.accent(spec.palette)
    lay = draw.layout(spec.layout)
    fb = fallback(cid, "", []) if accent is None or lay is None else None
    return replace(
        spec,
        motif=motifs.resolve(spec.motif) or motifs.FALLBACK,
        palette=accent.id if accent else fb.palette if fb else "",
        layout=lay if lay else fb.layout if fb else "",
    )


def object_in(raw: str) -> str | None:
    """The first `{` to the last `}`: past a code fence or a sentence before
    it."""
    a, b = raw.find("{"), raw.rfind("}")
    return raw[a : b + 1] if a != -1 and a < b else None


def one_line(s: str) -> str:
    return " ".join(s.split())


def trimmed(s: str) -> str:
    return one_line(s).strip("\"'*“”‘’`#").rstrip(".;:,").strip()


def clean_topic(raw: str) -> str | None:
    """A topic as a cover prints it: one line of a few words. A long title is
    clipped on a word; None when nothing is left."""
    t = trimmed(raw)
    if not t:
        return None
    words = t.split()
    if len(words) > TOPIC_MAX_WORDS:
        t = " ".join(words[:TOPIC_MAX_WORDS])
    return clip_chars(t, TOPIC_MAX_CHARS)


def _ascii_lower(s: str) -> str:
    return "".join(c.lower() if c.isascii() else c for c in s)


def clean_terms(raw: Any) -> list[str]:
    """Up to `TERMS_MAX` distinct terms, each a few words."""
    out: list[str] = []
    for r in raw:
        t = trimmed(str(r))
        t = clip_chars(" ".join(t.split()[:TERM_MAX_WORDS]), TERM_MAX_CHARS)
        if t and not any(_ascii_lower(o) == _ascii_lower(t) for o in out):
            out.append(t)
        if len(out) == TERMS_MAX:
            break
    return out


def clip_chars(s: str, max: int) -> str:
    """At most `max` characters, cut on a word when there is one to cut on."""
    if len(s) <= max:
        return s
    cut = s[:max]
    i = cut.rfind(" ")
    # Measured in bytes, as the Rust port measured it, so a cut lands where
    # it did there.
    if i != -1 and len(cut[:i].encode("utf-8")) > max // 2:
        return cut[:i].rstrip()
    return cut


# ── the model call ────────────────────────────────────────────────────────────


def system_prompt(language: str) -> str:
    """The system prompt: the three lists and what each choice suits."""
    names = ", ".join(motifs.names())
    palettes = "\n".join(f"- {a.id}: {a.mood}" for a in draw.ACCENTS)
    layouts = "\n".join(f"- {id}: {what}" for id, what in draw.LAYOUTS)
    return (
        "You design the cover of a collection of study material from what it contains. "
        "Reply with one JSON object and nothing else, no code fence:\n"
        '{"topic": "...", "terms": ["..."], "motif": "...", "palette": "...", '
        '"layout": "..."}\n\n'
        "topic: the subject as a cover would print it, 2 to 5 words, title case, no quotes. "
        "Not the collection title when that is long.\n"
        "terms: 3 to 5 key terms from the content, 1 to 3 words each.\n"
        f"motif: the one symbol that best pictures the subject, exactly one of: {names}. "
        "Pick the closest picture of the subject itself: plants and photosynthesis are tree "
        "or flower1, space is rocket or stars, speech models are mic, visas and jobs are "
        "briefcase, history is bank. collection is a last resort, only when nothing on the "
        "list is even close.\n"
        f"palette: the cover's one colour, exactly one of:\n{palettes}\n"
        f"layout: exactly one of:\n{layouts}\n"
        f"{language}"
    )


def current_version(
    cid: str, title: str, stored: Any, covers_on: bool, source_names: list[str]
) -> str:
    """The version the cover drawn now has, without reading any source: the
    designed spec's while covers are on, else the fallback's."""
    spec = CoverSpec.from_json(stored) if covers_on else None
    return version(spec) if spec else fallback_version(cid, title, source_names)
