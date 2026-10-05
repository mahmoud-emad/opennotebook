"""A question answered from a collection's sources, with every claim cited.
A port of `opennotebook_script/src/cite.rs`.

The sources are read whole: `grounding.excerpts_from` picks the passages that
share the most words with the question, they are numbered, and the model is
told to put the number of a passage after each claim it rests on. What comes
back is checked, not trusted: a number that names no passage is removed, and
the rest are renumbered in the order they are first used, so the reader sees
`[1]` before `[2]`. See `docs/mindmap-spec.md` section 5.
"""

import re
from dataclasses import dataclass, field

from opennotebook.script import generate, grounding
from opennotebook.script.errors import Empty
from opennotebook.script.mindmap import NamedDoc

# Passages given to the model. About 7,000 characters of material.
PASSAGES = 8

STAGE = "answer"

_NUMBER = re.compile(r"[0-9]+")


@dataclass
class Cited:
    """One cited passage, numbered as the answer's text numbers it."""

    # The number in the answer, from 1.
    n: int
    # Index into the documents the answer was asked over.
    doc: int
    excerpt: str


@dataclass
class Answer:
    # Markdown, with `[n]` markers that match `cited`.
    text: str
    # Only the passages the text cites, in order of first use.
    cited: list[Cited] = field(default_factory=list[Cited])


async def answer(docs: list[NamedDoc], question: str, *, model: str, language_rule: str) -> Answer:
    """Answer `question` from `docs` on `model`."""
    passages = grounding.excerpts_from([d.text for d in docs], question, PASSAGES)
    if not passages:
        raise Empty(STAGE)
    raw = await generate.send(
        model, system_prompt(language_rule), user_prompt(docs, passages, question), STAGE
    )
    text, order = renumber(raw, len(passages))
    cited = [
        Cited(n=i + 1, doc=passages[p][0], excerpt=passages[p][1]) for i, p in enumerate(order)
    ]
    return Answer(text, cited)


def system_prompt(language_rule: str) -> str:
    s = (
        "You answer questions about a person's source material, using only the numbered "
        "passages given.\n"
        "Rules:\n"
        "- After each claim, put the number of the passage it rests on in square brackets, "
        "like [2], or [2][5] for more than one. Every claim gets one.\n"
        "- Use only what the passages say. When they do not cover the question, say so in "
        "one sentence and stop.\n"
        "- A few short paragraphs, or a short list when the answer is a list. No heading, "
        "no preamble, no closing summary."
    )
    if language_rule:
        s += f"\n\n{language_rule} Keep the [n] markers exactly as described."
    return s


def user_prompt(docs: list[NamedDoc], passages: list[tuple[int, str]], question: str) -> str:
    s = "Passages:\n\n"
    for i, (doc, text) in enumerate(passages):
        title = docs[doc].title.strip() if doc < len(docs) else ""
        s += f"[{i + 1}] ({title}) {text.strip()}\n\n"
    return s + f"Question: {question.strip()}"


def marker_numbers(inner: str) -> list[int] | None:
    """The numbers of a citation marker's inside, `3` or `3, 9, 1`, or None
    when it is not one."""
    parts = [p.strip() for p in inner.split(",")]
    if not inner.strip() or not all(_NUMBER.fullmatch(p) for p in parts):
        return None
    return [int(p) for p in parts]


def renumber(reply: str, passages: int) -> tuple[str, list[int]]:
    """The reply with its citation markers checked and renumbered, and the
    passage index (from 0) behind each new number, in order.

    A marker is `[n]` or `[n, m]`. A number outside `1..=passages` is removed,
    and a marker left with no number is removed whole, together with the
    space before it. A list marker comes out as separate markers, `[1][2]`,
    which is the form the chat draws as chips. `[text](url)` is a link, not a
    marker, and is left alone.
    """
    order: list[int] = []
    out: list[str] = []
    rest = reply
    while (open_ := rest.find("[")) >= 0:
        out.append(rest[:open_])
        frm = rest[open_:]
        close = frm.find("]")
        if close < 0:
            out.append(frm)
            rest = ""
            break
        nums = marker_numbers(frm[1:close])
        if nums is None or frm[close + 1 :].startswith("("):
            out.append("[")
            rest = frm[1:]
            continue
        new = ""
        for n in nums:
            if n == 0 or n > passages:
                continue
            if n - 1 not in order:
                order.append(n - 1)
            marker = f"[{order.index(n - 1) + 1}]"
            if marker not in new:
                new += marker
        if not new:
            joined = "".join(out).rstrip(" ")
            out = [joined]
        out.append(new)
        rest = frm[close + 1 :]
    out.append(rest)
    return "".join(out).strip(), order
