"""A transformation: an instruction run over a collection's sources, with
every claim cited like an answer from Ask.

Ask picks the few passages that share the most words with a question. An
instruction such as "summarize" shares none with the sources, so here the
model is given passages from across all of them: every one when they fit,
otherwise an even spread, or the ones about the focus when one is given.
What comes back is checked as an answer is, by `cite.renumber`.
"""

from opennotebook.script import cite, generate, grounding
from opennotebook.script.errors import Empty
from opennotebook.script.mindmap import NamedDoc

STAGE = "transformation"

# About this much source text goes to the model: some 15,000 tokens.
BUDGET_CHARS = 60_000


def choose(docs: list[NamedDoc], focus: str) -> list[tuple[int, str]]:
    """The passages to work from, with the index of the document of each, in
    reading order."""
    texts = [d.text for d in docs]
    every = grounding.passages(texts)
    if sum(len(p) for _, p in every) <= BUDGET_CHARS:
        return every
    keep = max(BUDGET_CHARS // grounding.EXCERPT_CHARS, 1)
    if focus.strip():
        return grounding.excerpts_from(texts, focus, keep)
    # An even spread, so the end of a long source is read as much as its start.
    step = len(every) / keep
    return [every[int(i * step)] for i in range(min(keep, len(every)))]


def system_prompt(language_rule: str) -> str:
    s = (
        "You work on a person's source material, using only the numbered passages given, "
        "and carry out the instruction you are given.\n"
        "Rules:\n"
        "- After each claim, put the number of the passage it rests on in square brackets, "
        "like [2], or [2][5] for more than one. Every claim gets one.\n"
        "- Use only what the passages say. Never add a fact they do not give.\n"
        "- Follow the shape the instruction asks for. Markdown lists, bold and short "
        "headings are fine when they help; no preamble and no closing remark.\n"
        "- When the passages do not cover the instruction, say so in one sentence and stop."
    )
    if language_rule:
        s += f"\n\n{language_rule} Keep the [n] markers exactly as described."
    return s


def user_prompt(
    docs: list[NamedDoc], passages: list[tuple[int, str]], instruction: str, focus: str
) -> str:
    s = "Passages:\n\n"
    for i, (doc, text) in enumerate(passages):
        title = docs[doc].title.strip() if doc < len(docs) else ""
        s += f"[{i + 1}] ({title}) {text.strip()}\n\n"
    s += f"Instruction: {instruction.strip()}"
    if focus.strip():
        s += f"\n\nFocus on: {focus.strip()}"
    return s


async def run(
    docs: list[NamedDoc], instruction: str, focus: str, *, model: str, language_rule: str
) -> cite.Answer:
    """Carry out `instruction` over `docs` on `model`."""
    passages = choose(docs, focus)
    if not passages:
        raise Empty(STAGE)
    raw = await generate.send(
        model,
        system_prompt(language_rule),
        user_prompt(docs, passages, instruction, focus),
        STAGE,
    )
    text, order = cite.renumber(raw, len(passages))
    cited = [
        cite.Cited(n=i + 1, doc=passages[p][0], excerpt=passages[p][1]) for i, p in enumerate(order)
    ]
    return cite.Answer(text, cited)
