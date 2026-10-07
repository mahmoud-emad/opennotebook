"""A video's explainer: a question asked at a moment of a video, answered
from what the viewer has just heard and seen and from the collection's
sources, with every claim cited (`docs/video-overview-spec.md`, the watch
page).

The video's script says what is on at the moment: the chapter, the scene
and what its board writes, and the narration up to it. The passages are
picked as the citation engine picks them (`cite.py`), by the question and
the narration around the moment together, so "what does this mean?" finds
the passages about *this*. What comes back is checked as an answer is:
a citation that names no passage is removed and the rest renumbered. The
answer may point to another moment as `[m:ss]`; one past the video's end
is removed.
"""

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from opennotebook.script import cite, generate, grounding
from opennotebook.script.mindmap import NamedDoc

STAGE = "explain"

# Passages given to the model: fewer than an answer gets, as the narration
# carries much of the context.
PASSAGES = 6
# The narration before the moment told as "just heard", in characters, and
# the turns of the thread kept.
HEARD_CHARS = 1_800
AHEAD_CHARS = 500
HISTORY_TURNS = 6
HISTORY_CHARS = 1_200
# The narration's timeline, for pointing to a moment: each line's start and
# its first words, at most this many lines, sampled evenly.
TIMELINE_LINES = 60
TIMELINE_WORDS = 10

Mode = Literal["ask", "explain", "example", "why", "quiz"]

# What each quick prompt asks, said as the viewer would.
ASKS: dict[str, str] = {
    "explain": "Explain what was just said, more simply and in a little more depth.",
    "example": "Give me a concrete example of what was just explained.",
    "why": "Why does what was just explained matter?",
    "quiz": (
        "Quiz me on what I have watched so far: one question at a time, "
        "multiple choice with three options. Do not give the answer yet."
    ),
}

MOMENT = re.compile(r"\[(\d{1,3}):([0-5]\d)\]")


@dataclass
class Cited:
    n: int
    doc: int
    excerpt: str


@dataclass
class Explained:
    # Markdown with `[n]` citations and `[m:ss]` moments.
    text: str
    cited: list[Cited] = field(default_factory=list[Cited])


@dataclass
class Moment:
    """What is on at a time of the video."""

    t_ms: int
    chapter: str
    scene: str
    board: list[str]
    claims: list[str]
    heard: str
    ahead: str


def clock(ms: int) -> str:
    s = max(ms, 0) // 1000
    return f"{s // 60}:{s % 60:02d}"


def _items(script: dict[str, Any], key: str) -> list[dict[str, Any]]:
    got = script.get(key)
    return [x for x in got if isinstance(x, dict)] if isinstance(got, list) else []  # pyright: ignore[reportUnknownVariableType]


def _on(items: list[dict[str, Any]], t: int) -> dict[str, Any] | None:
    """The item on at `t`: the last to start by then."""
    on = None
    for it in items:
        if int(it.get("start_ms", 0)) <= t:
            on = it
    return on


def moment(script: dict[str, Any], t_ms: int) -> Moment:
    t = max(0, min(t_ms, int(script.get("duration_ms", t_ms) or t_ms)))
    chapter = _on(_items(script, "chapters"), t) or {}
    scene = _on(_items(script, "scenes"), t) or {}
    lines = _items(script, "lines")
    heard = " ".join(str(ln.get("text", "")) for ln in lines if int(ln.get("start_ms", 0)) <= t)
    ahead = " ".join(str(ln.get("text", "")) for ln in lines if int(ln.get("start_ms", 0)) > t)
    return Moment(
        t_ms=t,
        chapter=str(chapter.get("title", "")),
        scene=str(scene.get("title", "")),
        board=[str(x) for x in scene.get("labels", []) or []],
        claims=[str(x) for x in scene.get("claims", []) or []],
        heard=heard[-HEARD_CHARS:],
        ahead=ahead[:AHEAD_CHARS],
    )


def system_prompt(language_rule: str) -> str:
    s = (
        "You are the tutor beside a video lesson. The viewer has paused it to ask you "
        "something. You know the lesson's script and the passages from the viewer's own "
        "sources that it was made from.\n"
        "Rules:\n"
        "- Answer about the moment they paused at, building on what they have just heard. "
        "Talk to them plainly, as a good teacher would.\n"
        "- Facts come from the numbered passages or the lesson. After a claim from a passage, "
        "put its number in square brackets, like [2]. When neither covers the question, say "
        "so in one sentence, then say what the lesson does cover.\n"
        "- To point to a moment of the video, write its time as [m:ss], like [1:24]; use only "
        "times from the chapter list or the timeline.\n"
        "- Short: two or three short paragraphs at most, or a short list. No heading, no "
        "preamble, no closing summary."
    )
    if language_rule:
        s += f"\n\n{language_rule} Keep the [n] and [m:ss] markers exactly as described."
    return s


def user_prompt(
    script: dict[str, Any],
    m: Moment,
    docs: list[NamedDoc],
    passages: list[tuple[int, str]],
    ask: str,
    history: list[tuple[str, str]],
) -> str:
    s = f"The lesson: {script.get('title', '')}\n\nChapters:\n"
    for c in _items(script, "chapters"):
        s += f"- [{clock(int(c.get('start_ms', 0)))}] {c.get('title', '')}\n"
    s += timeline(script)
    s += f"\nPaused at {clock(m.t_ms)}"
    if m.chapter:
        s += f", in the chapter “{m.chapter}”"
    s += ".\n"
    if m.scene or m.board:
        s += f"On the board: {m.scene}"
        if m.board:
            s += " — " + "; ".join(m.board)
        s += "\n"
    if m.claims:
        s += "The scene shows: " + "; ".join(m.claims) + "\n"
    s += f"\nJust heard: …{m.heard}\n"
    if m.ahead:
        s += f"\nComing next (not heard yet): {m.ahead}…\n"
    if passages:
        s += "\nPassages:\n\n"
        for i, (doc, text) in enumerate(passages):
            title = docs[doc].title.strip() if doc < len(docs) else ""
            s += f"[{i + 1}] ({title}) {text.strip()}\n\n"
    if history:
        s += "\nThe conversation so far:\n"
        for who, text in history[-HISTORY_TURNS:]:
            s += f"{'Viewer' if who == 'user' else 'You'}: {text[:HISTORY_CHARS]}\n"
    return s + f"\nViewer: {ask}"


def timeline(script: dict[str, Any]) -> str:
    """The narration as a timeline: each line's time and first words."""
    lines = _items(script, "lines")
    if not lines:
        return ""
    step = max(1, -(-len(lines) // TIMELINE_LINES))
    s = "\nTimeline:\n"
    for ln in lines[::step]:
        words = str(ln.get("text", "")).split()
        more = "…" if len(words) > TIMELINE_WORDS else ""
        s += f"- [{clock(int(ln.get('start_ms', 0)))}] {' '.join(words[:TIMELINE_WORDS])}{more}\n"
    return s


def moments(text: str, duration_ms: int) -> str:
    """The answer with every `[m:ss]` past the video's end removed."""

    def keep(mt: re.Match[str]) -> str:
        ms = (int(mt.group(1)) * 60 + int(mt.group(2))) * 1000
        return mt.group(0) if ms <= duration_ms else ""

    return MOMENT.sub(keep, text)


async def explain(
    docs: list[NamedDoc],
    script: dict[str, Any],
    t_ms: int,
    question: str,
    mode: Mode,
    history: list[tuple[str, str]],
    *,
    model: str,
    language_rule: str,
) -> Explained:
    m = moment(script, t_ms)
    ask = question.strip() if mode == "ask" else ASKS[mode]
    query = " ".join([question, m.scene, *m.board, m.heard[-600:]])
    passages = grounding.excerpts_from([d.text for d in docs], query, PASSAGES) if docs else []
    raw = await generate.send(
        model,
        system_prompt(language_rule),
        user_prompt(script, m, docs, passages, ask, history),
        STAGE,
    )
    text, order = cite.renumber(raw, len(passages))
    text = moments(text, int(script.get("duration_ms", 0) or 0))
    cited = [
        Cited(n=i + 1, doc=passages[p][0], excerpt=passages[p][1]) for i, p in enumerate(order)
    ]
    return Explained(text, cited)
