"""A collection's sources as study notes: a written study guide of the key
ideas, every claim cited to the passage it rests on. A port of
`opennotebook_script/src/notes.rs`.

The design and the evidence behind it are in `docs/study-notes-spec.md`. In
short, it is NotebookLM's study guide and briefing doc in one: an overview,
the key ideas, a glossary of key terms, a short-answer quiz with its answers,
and essay questions. The whole of every source goes to the model as numbered
passages, so a citation can point at any passage and not only at the few a
question would retrieve.

What comes back is checked, not trusted, the way `cite` checks an answer:

- a number that names no passage is removed;
- a citation whose claim shares no word with the passage it names is removed
  and counted, because a model that has lost track of its numbering cites
  confidently and wrongly;
- the rest are renumbered in reading order, so the reader meets `[1]` first.

The model writes Markdown under fixed headings, and the sections are read out
of it here. A heading the model renames ("Key concepts" for "Key ideas") is
still found; a section it leaves out is simply empty.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum, auto

from opennotebook.script import cite, generate, grounding
from opennotebook.script.errors import Empty, Truncated
from opennotebook.script.mindmap import NamedDoc, lines

log = logging.getLogger(__name__)

# Above this much source text the sources are cut to excerpts. The same
# ceiling as the mind map's: about 150k tokens, far past any real collection.
WHOLE_TEXT_CHARS = 600_000

# The fewest key ideas worth showing without asking once more.
MIN_IDEAS = 2

STAGE = "study notes"


@dataclass
class Idea:
    """One key idea: a heading and what the sources say about it, in Markdown
    with `[n]` markers."""

    heading: str
    body: str = ""


@dataclass
class Term:
    term: str
    definition: str


@dataclass
class Question:
    """One short-answer question and the answer the sources give."""

    question: str
    answer: str = ""


@dataclass
class Cited:
    """One cited passage, numbered as the notes number it."""

    # The number in the notes, from 1.
    n: int
    # Index into the documents the notes were made from.
    doc: int
    excerpt: str


@dataclass
class StudyNotes:
    """What a generation produced, with what it had to do to get there."""

    title: str = ""
    overview: str = ""
    ideas: list[Idea] = field(default_factory=list[Idea])
    glossary: list[Term] = field(default_factory=list[Term])
    quiz: list[Question] = field(default_factory=list[Question])
    essays: list[str] = field(default_factory=list[str])
    # Only the passages the notes cite, in order of first use.
    cited: list[Cited] = field(default_factory=list[Cited])
    # Citations removed: a number naming no passage, or a passage that shares
    # no word with the claim citing it.
    dropped: int = 0
    # The sources were cut to excerpts to fit one call.
    excerpted: bool = False
    # The citation check did not run, because the notes are in another
    # language than English and cannot be matched word for word.
    unchecked: bool = False
    model: str = ""

    def to_markdown(self, source_title: Callable[[int], str]) -> str:
        """The notes as one Markdown document, `[n]` markers and all, with the
        cited sources listed at the end. The export, and what an agent reads."""
        out = f"# {self.title}\n\n"
        if self.overview:
            out += f"## Overview\n\n{self.overview}\n\n"
        if self.ideas:
            out += "## Key ideas\n\n"
            for i in self.ideas:
                out += f"### {i.heading}\n\n{i.body}\n\n"
        if self.quiz:
            out += "## Quiz\n\n"
            out += "".join(f"{k + 1}. {q.question}\n" for k, q in enumerate(self.quiz))
            out += "\n## Answer key\n\n"
            out += "".join(f"{k + 1}. {q.answer}\n" for k, q in enumerate(self.quiz))
            out += "\n"
        if self.essays:
            out += "## Essay questions\n\n"
            out += "".join(f"{k + 1}. {e}\n" for k, e in enumerate(self.essays))
            out += "\n"
        if self.glossary:
            out += "## Glossary\n\n"
            out += "".join(f"- **{t.term}**: {t.definition}\n" for t in self.glossary)
            out += "\n"
        if self.cited:
            out += "## Sources\n\n"
            for c in self.cited:
                short = " ".join(c.excerpt.split())
                if len(short) > 200:
                    short = short[:200] + "…"
                out += f'{c.n}. {source_title(c.doc)}: "{short}"\n'
        return out.rstrip() + "\n"


async def generate_notes(
    sources: list[NamedDoc],
    title_hint: str,
    focus: str | None,
    *,
    model: str,
    language_rule: str,
) -> StudyNotes:
    """Make study notes of `sources` on `model`.

    `title_hint` names the notes when the model writes no title. `focus` is
    the person's own words, or nothing.
    """
    check = not language_rule
    passages, excerpted = numbered(sources, title_hint)
    if not passages:
        raise Empty(STAGE)
    system = system_prompt(focus, language_rule)
    user = user_prompt(sources, passages, focus)

    best: StudyNotes | None = None
    for attempt in range(2):
        asked = (
            user
            if attempt == 0
            else f"{user}\n\nYour last notes were missing their sections. Write every section "
            "under its exact heading — ## Overview, ## Key ideas with a ### heading per idea, "
            "## Quiz, ## Answer key, ## Essay questions, ## Glossary — and cite with [n]."
        )
        # A reply cut at the token ceiling keeps the sections before the cut:
        # notes missing their essay questions beat no notes.
        try:
            raw = await generate.send(model, system, asked, STAGE)
        except Truncated as e:
            if not e.text.strip():
                raise
            raw = e.text
        notes = read(raw, passages, title_hint, check)
        enough = len(notes.ideas) >= MIN_IDEAS
        if best is None or weight(notes) > weight(best):
            best = notes
        if enough:
            break
        log.warning("notes attempt %d came back thin; reply starts:\n%s", attempt + 1, raw[:800])

    if best is None or not (best.ideas or best.overview):
        raise Empty(STAGE)
    best.excerpted = excerpted
    best.unchecked = not check
    best.model = model
    return best


def weight(n: StudyNotes) -> int:
    """How much a set of notes holds, to keep the fuller of two attempts."""
    return 3 * len(n.ideas) + len(n.glossary) + len(n.quiz) + len(n.essays)


def read(raw: str, passages: list[tuple[int, str]], title_hint: str, check: bool) -> StudyNotes:
    """A reply, checked and read into its sections."""
    checked, unsupported = check_citations(raw, passages) if check else (raw, 0)
    invalid = count_invalid(checked, len(passages))
    text, order = cite.renumber(checked, len(passages))
    notes = parse_sections(text, title_hint)
    notes.cited = [
        Cited(n=i + 1, doc=passages[p][0], excerpt=passages[p][1]) for i, p in enumerate(order)
    ]
    notes.dropped = unsupported + invalid
    return notes


def numbered(sources: list[NamedDoc], query: str) -> tuple[list[tuple[int, str]], bool]:
    """Every passage of every source, numbered from 1 by position in this
    list, with the source it came from. Cut to the passages about the title
    when the sources are too long to send whole; the second value says so."""
    texts = [s.text for s in sources]
    if sum(len(t) for t in texts) <= WHOLE_TEXT_CHARS:
        return grounding.passages(texts), False
    # An even share each, so a long source does not crowd out a short one.
    keep = max(WHOLE_TEXT_CHARS // max(len(sources), 1) // 900, 1)
    return [(i, p) for i, t in enumerate(texts) for p in grounding.excerpts([t], query, keep)], True


def system_prompt(focus: str | None, language_rule: str) -> str:
    focus_rule = (
        "\n- A focus is given after the passages. Build the notes around it, and leave out "
        "what does not bear on it."
        if focus and focus.strip()
        else ""
    )
    # The sections and their sizes are NotebookLM's study guide — short-answer
    # quiz, its answer key apart, essay questions, glossary of key terms —
    # with its briefing doc's overview and key ideas in front, because the
    # tile promises "the key ideas". The citation rules are SurfSense's, which
    # are the plainest of the open implementations read for the spec.
    s = (
        "You write study notes from a person's source material: a study guide that first "
        "teaches the key ideas, then lets them test themselves, the way a good teacher's "
        "handout does.\n\n"
        "The material comes as numbered passages, [1], [2] and so on. How to cite:\n"
        "1. Put the passage's number in square brackets right after the claim it supports: "
        "[12].\n"
        "2. Several passages for one claim: stack them, [12][40].\n"
        "3. Copy the numbers exactly as shown. Never renumber them or make one up, and never "
        "write a title, link or footnote instead.\n"
        "4. Cite the passage that actually says it. If no passage backs a claim, leave the "
        "claim out.\n"
        "5. No list of references at the end: the numbers are the references.\n\n"
        "Write Markdown: exactly these sections, in this order, under exactly these headings.\n"
        "# <a title for the notes, at most 10 words>\n"
        "## Overview\n"
        "Two to four sentences: what the material is about and why it matters. Cited.\n"
        "## Key ideas\n"
        "Four to seven ideas, the most important first, each as:\n"
        "### <the idea, as a short phrase>\n"
        "Two to five sentences, or a short list, explaining it with the material's own facts, "
        "numbers and examples. Every claim cited.\n"
        "## Quiz\n"
        "Ten short-answer questions, numbered 1 to 10, each answerable in two or three "
        "sentences from the material. Test understanding — why, how, what follows — not only "
        "recall, and spread them across the key ideas.\n"
        "## Answer key\n"
        "Ten answers, numbered 1 to 10 to match the questions, two or three sentences each. "
        "Every answer cited.\n"
        "## Essay questions\n"
        "Five open questions, numbered, that make the reader connect several ideas. No "
        "answers.\n"
        "## Glossary\n"
        "Fifteen to twenty key terms the material uses, one per line:\n"
        "- **Term**: a one or two sentence definition, as the material uses it. Cited.\n\n"
        "Rules:\n"
        "- Use only what the passages say. Do not add facts from your own knowledge. If the "
        "material is thin on something, say less rather than fill in.\n"
        "- Plain, clear language for a learner. Define a term the first time it is used.\n"
        "- The quiz and essays test the key ideas; they do not introduce new facts.\n"
        "- Do not start every bullet with the same word.\n"
        f"- No preamble, no closing remarks, nothing outside the sections.{focus_rule}"
    )
    if language_rule:
        s += (
            f"\n\n{language_rule} Keep the Markdown headings' structure and the [n] markers "
            "exactly as described."
        )
    return s


def user_prompt(sources: list[NamedDoc], passages: list[tuple[int, str]], focus: str | None) -> str:
    s = "Passages:\n\n"
    for i, (doc, text) in enumerate(passages):
        title = sources[doc].title.strip() if doc < len(sources) else ""
        s += f"[{i + 1}] ({title}) {text.strip()}\n\n"
    if f := (focus or "").strip():
        s += f"Focus: {f}\n"
    return s


# ── the citation check ────────────────────────────────────────────────────────


def _marker(frm: str, close: int) -> list[int] | None:
    """The numbers of the marker `frm[:close + 1]`, or None when it is not a
    citation marker: not numbers, or a link's text."""
    nums = cite.marker_numbers(frm[1:close])
    return None if nums is None or frm[close + 1 :].startswith("(") else nums


def check_citations(raw: str, passages: list[tuple[int, str]]) -> tuple[str, int]:
    """The reply with every citation whose claim shares no word with its
    passage removed, and how many were.

    The claim is the text between the previous marker or sentence end and the
    marker. A claim of fewer than two matchable words ("See [3].") is not
    judged, because there is nothing to match. A word matches when it, or the
    word without a final "s", appears in the passage. A number that names no
    passage is left alone here: `renumber` removes those, and they are counted
    apart.
    """
    lowered = [p.lower() for _, p in passages]
    dropped = 0
    out = ""
    claim_from = 0
    rest = raw
    while (open_ := rest.find("[")) >= 0:
        out += rest[:open_]
        frm = rest[open_:]
        close = frm.find("]")
        if close < 0:
            out += frm
            rest = ""
            break
        numbers = _marker(frm, close)
        if numbers is None:
            out += "["
            rest = frm[1:]
            continue
        claim = claim_before(out[claim_from:])
        words = grounding.terms(claim)
        kept: list[int] = []
        for n in numbers:
            if not 1 <= n <= len(lowered):
                kept.append(n)
                continue
            p = lowered[n - 1]
            if len(words) < 2 or supports(p, words):
                kept.append(n)
                continue
            dropped += 1
            # The service log is where a wrong drop shows up: the claim and
            # the passage it named, side by side.
            log.info('notes: dropped [%d] for "%s" — passage: "%s"', n, claim.strip(), p[:160])
        if kept:
            out += "[" + ", ".join(str(n) for n in kept) + "]"
        else:
            out = out.rstrip(" ")
        # A run of markers, "[2][5]", shares one claim.
        if not frm[close + 1 :].startswith("["):
            claim_from = len(out)
        rest = frm[close + 1 :]
    return out + rest, dropped


def claim_before(text: str) -> str:
    """The last sentence or line of `text`: the claim a marker at its end
    cites."""
    t = text.rstrip()
    for i in range(len(t) - 1, -1, -1):
        c = t[i]
        if c == "\n" or (c in ".!?" and i + 1 < len(t) and t[i + 1].isspace()):
            return t[i + 1 :]
    return t


def supports(passage: str, words: list[str]) -> bool:
    """Whether a passage carries enough of a claim's words to be what it
    cites: two distinct words, or one of a claim of three or fewer.

    One shared word is not enough: "Mimi is a neural audio codec" matched a
    passage about audio TOKENS on "audio" alone. A ratio alone is too much: a
    long paraphrase — "Helium is a text LLM that serves as Moshi's backbone,
    providing it with reasoning abilities…", fourteen words — shares four
    with the passage that introduces Helium, under three in ten, and was
    dropped from a correct citation. Measured on the Moshi paper, the ratio
    alone dropped 22 citations, about half of them right ones.
    """
    hit = sum(1 for w in words if mentions(passage, w))
    return hit >= 2 or (hit == 1 and len(words) <= 3)


def mentions(passage: str, word: str) -> bool:
    return word in passage or (word.endswith("s") and len(word) - 1 >= 3 and word[:-1] in passage)


def count_invalid(text: str, passages: int) -> int:
    """Citation numbers in `text` that name no passage: what `renumber` is
    about to remove."""
    n = 0
    rest = text
    while (open_ := rest.find("[")) >= 0:
        frm = rest[open_:]
        close = frm.find("]")
        if close < 0:
            break
        if (nums := _marker(frm, close)) is not None:
            n += sum(1 for k in nums if k == 0 or k > passages)
        rest = frm[1:]
    return n


# ── reading the sections ──────────────────────────────────────────────────────


class Section(Enum):
    NONE = auto()
    OVERVIEW = auto()
    IDEAS = auto()
    GLOSSARY = auto()
    QUIZ = auto()
    ANSWERS = auto()
    ESSAYS = auto()


def section_of(heading: str) -> Section:
    """Which section a `##` heading opens. Read loosely: a model that writes
    "Key concepts", "Short-answer quiz" or "Glossary of key terms" means the
    same sections. Answers are checked before the quiz, because "Quiz answer
    key" names both, and "Short-answer questions" is the quiz, not its key."""
    h = heading.lower()

    def has(*words: str) -> bool:
        return any(w in h for w in words)

    if has("answer") and not has("question", "short-answer", "short answer"):
        return Section.ANSWERS
    if has("essay", "discussion", "reflection"):
        return Section.ESSAYS
    if has("quiz", "question", "self-test", "test yourself", "check your"):
        return Section.QUIZ
    if has("glossary", "term", "vocabulary", "definition"):
        return Section.GLOSSARY
    if has("overview", "summary", "introduction", "at a glance"):
        return Section.OVERVIEW
    if has("idea", "concept", "theme", "point", "takeaway", "insight"):
        return Section.IDEAS
    return Section.NONE


def heading(line: str) -> tuple[int, str] | None:
    """A heading line's level and text: `## Quiz` is (2, "Quiz"). A bold line
    on its own, `**Quiz**`, is read as level 2, which is how a model writes a
    heading it has forgotten the hashes for."""
    t = line.strip()
    hashes = len(t) - len(t.lstrip("#"))
    if hashes > 0 and t[hashes:].startswith(" "):
        return hashes, clean_heading(t[hashes:])
    if len(t.encode()) > 4 and t.startswith("**") and t.endswith("**") and "**" not in t[2:-2]:
        return 2, clean_heading(t[2:-2])
    return None


def clean_heading(h: str) -> str:
    return h.strip().strip("*_:#").strip()


def item(line: str) -> str | None:
    """A list item's text without its marker: `- x`, `* x`, `1. x`, `1) x`."""
    t = line.lstrip()
    for b in ("- ", "* ", "• "):
        if t.startswith(b):
            return t[len(b) :].strip()
    digits = len(t) - len(t.lstrip("0123456789"))
    if 0 < digits <= 3:
        r = t[digits:]
        for sep in (". ", ") "):
            if r.startswith(sep):
                return r[len(sep) :].strip()
    return None


def term_of(text: str) -> Term | None:
    """A glossary line as its term and definition: `**Term**: def`,
    `Term: def`, `Term — def`."""
    t = text.strip()
    if t.startswith("**"):
        r = t[2:]
        end = r.find("**")
        if end < 0:
            return None
        term, definition = r[:end].rstrip(":"), r[end + 2 :].lstrip(":-—– ")
    elif ": " in t:
        term, _, definition = t.partition(": ")
    elif " — " in t:
        term, _, definition = t.partition(" — ")
    elif " - " in t:
        term, _, definition = t.partition(" - ")
    else:
        return None
    term = term.strip().strip("*").strip()
    definition = definition.strip()
    if not term or not definition or len(term) > 80:
        return None
    return Term(term, definition)


def inline_answer(text: str) -> str | None:
    """A quiz line's answer, when the model wrote it under its question:
    `Answer: …` or `A: …`."""
    t = text.strip().lstrip("*_")
    for p in ("Answer:", "answer:", "A:", "Answer**:", "Answer:**"):
        if t.startswith(p):
            return t[len(p) :].strip().lstrip("*_").strip()
    return None


def _ascii_lower(s: str) -> str:
    return "".join(c.lower() if c.isascii() else c for c in s)


def parse_sections(text: str, title_hint: str) -> StudyNotes:
    """The notes' sections, read out of the Markdown."""
    n = StudyNotes()
    section = Section.NONE
    overview: list[str] = []
    answers: list[str] = []
    for line in lines(text):
        if (h := heading(line)) is not None:
            level, name = h
            if level == 1:
                if not n.title and name:
                    n.title = name
                continue
            if level >= 3 and section == Section.IDEAS:
                n.ideas.append(Idea(name))
                continue
            s = section_of(name)
            if s != Section.NONE:
                section = s
                continue
            # An unknown `##` heading inside the key ideas is an idea a model
            # wrote one level too high.
            if section == Section.IDEAS:
                n.ideas.append(Idea(name))
            continue
        t = line.strip()
        match section:
            case Section.NONE:
                pass
            case Section.OVERVIEW:
                if t:
                    overview.append(t)
            case Section.IDEAS:
                if n.ideas:
                    idea = n.ideas[-1]
                    if idea.body or t:
                        idea.body += line.rstrip() + "\n"
                elif t:
                    # Body text before any idea heading: an overview the model
                    # put in the wrong place, kept rather than lost.
                    overview.append(t)
            case Section.GLOSSARY:
                listed = item(line)
                term = term_of(listed) if listed is not None else None
                if term is None and t:
                    term = term_of(t)
                if term is not None:
                    n.glossary.append(term)
            case Section.QUIZ:
                if (q := item(line)) is not None:
                    if (a := inline_answer(q)) is not None:
                        if n.quiz:
                            n.quiz[-1].answer = a
                    else:
                        n.quiz.append(Question(q))
                elif (a := inline_answer(t)) is not None and n.quiz:
                    n.quiz[-1].answer = a
            case Section.ANSWERS:
                if (a := item(line)) is not None:
                    said = inline_answer(a)
                    answers.append(a if said is None else said)
                elif t and answers:
                    answers[-1] += " " + t
            case Section.ESSAYS:
                if (e := item(line)) is not None:
                    n.essays.append(e)
    # The answer key, matched to the questions by position.
    for q, a in zip(n.quiz, answers, strict=False):
        if not q.answer:
            q.answer = a
    # Alphabetical, as a glossary is read; the same term twice keeps the first.
    glossary: list[Term] = []
    for g in sorted(n.glossary, key=lambda g: g.term.lower()):
        if not glossary or _ascii_lower(glossary[-1].term) != _ascii_lower(g.term):
            glossary.append(g)
    n.glossary = glossary
    n.overview = " ".join(overview)
    for i in n.ideas:
        i.body = i.body.strip()
    n.ideas = [i for i in n.ideas if i.heading and i.body]
    if not n.title:
        n.title = title_hint
    return n
