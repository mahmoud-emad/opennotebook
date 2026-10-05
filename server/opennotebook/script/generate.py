"""Outline, then the script, both grounded in the collection's sources. A
port of `opennotebook_script/src/generate.rs`.

Three things section 3 does not specify are decided here and flagged rather
than buried: how many slides a session gets, how long a slide's narration
runs, and what two speakers do differently.

One speaker and two speakers share one code path. There is no branch on
speaker count in the parsing or the budgets: the speaker set is data, and a
one-speaker session rotates over one.
"""

import logging
import re
from dataclasses import dataclass, field

from opennotebook.ai import client
from opennotebook.ai.errors import AiError
from opennotebook.domain import settings as st
from opennotebook.domain.sessions import AudioSpec, Line, Part, Speaker
from opennotebook.script import budget
from opennotebook.script.errors import Empty, ScriptError, Truncated
from opennotebook.script.parse import (
    LAYOUTS,
    ScriptedLine,
    layout_of,
    parse_lines,
    split_slide_reply,
)
from opennotebook.script.retrieval import Scope, retrieve

log = logging.getLogger(__name__)


def truncated(finish_reason: str | None) -> bool:
    f = (finish_reason or "").lower()
    return "length" in f or "max_token" in f


async def send(model: str, system: str, user: str, stage: str) -> str:
    """The model's reply to one system and one user message.

    Raises `Truncated` when the model stopped at its token ceiling, with what
    it wrote before the cut, and `Empty` when it wrote nothing. The call is
    charged either way: the client writes it to the ledger before these
    checks, because a truncated or empty reply was billed too.

    `finish_reason` is the fourth silent-empty case in this stack: a model
    that runs out of room returns prose that reads whole. Nothing in the text
    shows it, so the text is never trusted without the reason beside it.
    """
    done = await client.ai().complete(
        model,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    if truncated(done.finish_reason):
        # The text rides along so a caller that can use a partial answer keeps
        # it. A caller that wants the strict behaviour simply ignores it.
        raise Truncated(stage, done.finish_reason or "", done.text)
    if not done.text.strip():
        raise Empty(stage)
    return done.text


class NoGrounding(ScriptError):
    """Nothing was retrieved to ground on. Generating from the model's own
    knowledge is the failure this module exists to avoid."""

    def __init__(self) -> None:
        super().__init__("no grounding found in the collection's sources")

    @property
    def sentence(self) -> str:
        return (
            "Nothing in this collection's sources could be read for the script. Add a source "
            "with some text in it, then try again."
        )


# Slides per session when the caller does not say. Five, the same default the
# slide count setting has (`settings.SLIDES_DEFAULT`): two defaults for one
# number would be a bug waiting to happen.
DEFAULT_SLIDE_COUNT = st.SLIDES_DEFAULT

# How many passages and pairs to retrieve per query.
TOP_K = 4

# How many points the plan gives a part.
POINTS_PER_PART = 3

# How many follow-up calls a slide may take to reach its length.
EXTEND_CALLS = 3

# How many of the previous slide's lines the next one is shown.
CARRIED_LINES = 2

# Characters of source material each part of the whole-session call carries.
#
# Retrieval returns whole passages, and some are long: the first live run
# asked for 309,138 tokens against the model's 128,000 and got a 400. One
# slide on its own fits, five together do not. 12,000 characters is about
# 3,000 tokens, so five parts plus the instructions stay near 20,000 — well
# inside the window, and more than a 650-character part can use.
MATERIAL_PER_PART = 12_000

# Two lines is a welcome; four is a preamble nobody asked for.
BOOKEND_LINES = 2

# How much of the script before a part the edit pass is shown.
REVIEW_CONTEXT = 12_000


@dataclass
class ScriptSpec:
    title: str
    # One speaker or two. The set is data; nothing below branches on its size
    # except how the conversation is described.
    speakers: list[Speaker]
    # The deck directory and presentation the slides will be written in.
    # Until the slides are written there is no deck, so these name where it
    # will go rather than where it is.
    deck_collection: str = ""
    deck_presentation: str = ""
    slide_count: int = DEFAULT_SLIDE_COUNT
    # Narration characters per slide; see `budget.slide_narration`.
    slide_narration: int = budget.SLIDE_NARRATION
    # The most `[image: …]` lines one slide may carry; None is no limit.
    images_per_slide: int | None = None
    # Present for an audio overview: no slides, the parts are chapters, and
    # the format decides how the hosts talk.
    audio: AudioSpec | None = None
    # The output language's English name, read once by the caller for the
    # whole build. Empty is English, which every prompt is written in.
    language: str = ""
    # The script model, read once by the caller: an operator setting, so a
    # build is written by one model even if the setting changes meanwhile.
    model: str = st.SCRIPT_MODEL_DEFAULT

    def adapt(self, prompt: str) -> str:
        """A system prompt written for a slide session, made right for an
        audio overview.

        One set of prompts serves both, because everything that makes the
        script good — the plan's owned points, the host dynamic, the edit
        pass — is the same. What differs is said here: there is no slide, so
        the instructions for its copy go and "slide" becomes "chapter"; the
        format and the focus are added at the end. A slide session's prompt
        passes through unchanged.
        """
        a = self.audio
        if a is None:
            return prompt
        out = f"{self.adapt_user(prompt)}\n\n{format_rules(a)}"
        if a.focus.strip():
            out += (
                f'\n\nThe listener asked for this focus, in their words: "{a.focus.strip()}". '
                "Build everything around it, and leave out what does not bear on it."
            )
        return out

    def adapt_user(self, prompt: str) -> str:
        """The wording half of `adapt`, for a user prompt: the slide copy
        instructions removed and "slide" said as "chapter"."""
        a = self.audio
        if a is None:
            return prompt
        p = prompt
        # The copy block of a one-slide prompt runs to the end of it.
        i = p.find("\n\nThen write the line `SLIDE:`")
        if i >= 0:
            p = p[:i]
        # The whole-session prompt's copy block sits in the middle.
        end = "no colours or fonts.\n\n"
        i, j = p.find("Then the line `SLIDE:`"), p.find(end)
        if 0 <= i < j:
            p = p[:i] + p[j + len(end) :]
        what = (
            f"an audio overview, a {a.label} episode about the listener's sources, heard and "
            "never seen"
        )
        return (
            p.replace("a short explanatory session", what)
            .replace("an explanatory session", what)
            .replace("a spoken explanatory session", what)
            .replace("explanatory session", "audio overview")
            .replace("is shown as", "is heard as")
            .replace("Slides", "Chapters")
            .replace("slides", "chapters")
            .replace("Slide", "Chapter")
            .replace("slide", "chapter")
        )

    def narration_target(self) -> int:
        """What the prompt asks for: a little under the budget, because the
        budget is a hard cut and a model told only a ceiling writes far less
        than a long session needs."""
        return self.slide_narration * 9 // 10

    def image_element(self) -> str:
        """The image element line for the slide-copy instructions, or none at
        all when a slide may not have an image."""
        if self.images_per_slide == 0:
            return ""
        if self.images_per_slide is not None:
            return (
                f"- [image: <what it depicts>] — <placement> (at most {self.images_per_slide} on "
                "a slide; leave it out when a Point or Stat already says it)\n"
            )
        return "- [image: <what it depicts>] — <placement>\n"

    def speaker_ids(self) -> list[str]:
        return [s.speaker_id for s in self.speakers]


# ── one call ─────────────────────────────────────────────────────────────────


async def complete(spec: ScriptSpec, system: str, user: str, stage: str) -> str:
    """One completion on the script model, with the language rule added.

    The rule is added here because every stage passes through this call. The
    format markers stay as written or the parser cannot read the reply.
    """
    rule = st.language_rule(spec.language)
    if rule:
        system = (
            f"{system}\n\n{rule} Keep speaker ids, markers such as `SLIDE:` and field labels "
            "exactly as written above; only the words themselves change language."
        )
    return await send(spec.model, system, user, stage)


async def complete_partial(spec: ScriptSpec, system: str, user: str, stage: str) -> str:
    """`complete`, but a truncated reply is kept rather than refused.

    Truncation is not emptiness. When the model runs into its token ceiling
    the lines BEFORE the cut are complete and usable, and everything
    downstream already trims: `budget.room` drops a line there is no space
    for, and `budget.whole_sentences` ends the last one on a sentence.
    Refusing the whole reply threw all of that away and killed a prep that
    had already paid for its outline.

    The outline still refuses a truncated reply, because there the cut costs
    whole SLIDES rather than the tail of one line, and a deck quietly missing
    its last two topics is the kind of success-shaped failure this project
    keeps meeting.
    """
    try:
        return await complete(spec, system, user, stage)
    except Truncated as e:
        if e.text.strip():
            return e.text
        raise


def example_id(spec: ScriptSpec) -> str:
    """One real id to show the model, so the format line has nothing generic
    in it to copy. A prompt that said "lines of the form `speaker_id: …`" got
    exactly that back, tagged `host_id`, and failed the prep."""
    return spec.speakers[0].speaker_id if spec.speakers else "host"


# ── the plan ─────────────────────────────────────────────────────────────────


@dataclass
class PlanPart:
    """One part of the plan: the slide's title and the points that part alone
    explains."""

    title: str
    points: list[str] = field(default_factory=list[str])

    def query(self) -> str:
        """What to retrieve this part's material with: its title and its
        points, so a part is grounded on what it explains rather than on a
        heading that four other parts share."""
        if not self.points:
            return self.title
        return f"{self.title}. {'. '.join(self.points)}"


def outline_shape(spec: ScriptSpec) -> str:
    """How an audio overview's format shapes its plan; nothing for a slide
    session."""
    f = spec.audio.format if spec.audio else None
    if f == "brief":
        return (
            "\nPlan it as a brief: the first part is the single most important finding, the "
            "last part the other key points and the takeaway."
        )
    if f == "critique":
        return (
            "\nPlan it as a critique: the first part is what the material sets out to do and "
            "its real strengths; each middle part is one weakness or risk, most important first, "
            "with what would fix it; the last part is the revisions in order of priority."
        )
    if f == "debate":
        return (
            "\nPlan it as a debate: the first part states the question and the two positions "
            "the material supports; each middle part is one contested point with the evidence "
            "on both sides; the last part is what each side must concede and what stays open."
        )
    return ""


async def outline(scope: Scope, spec: ScriptSpec) -> list[PlanPart]:
    """Section 3 step 2: one call, the whole collection as context, out comes
    the plan: a title per slide and the points each one owns.

    Titles alone were not enough to keep the parts apart. Five titles on a
    paper about Moshi all led back to its headline idea, and each part,
    knowing only its own title, explained that idea again: a real session
    explained "Inner Monologue" on all five slides and the latency figure on
    three. NotebookLM's open reimplementations plan first for the same reason
    — the sources become a list of key points, and the script walks through
    them once. A point is owned by exactly one part, so every other part
    knows not to explain it.
    """
    context = await retrieve(scope, spec.title, TOP_K * 3)
    if context.is_empty():
        raise NoGrounding
    system = (
        "You plan a short explanatory session from source material, the way a good teacher "
        "plans a lesson: what the listener needs first, what builds on it, and what they "
        "should walk away with.\n"
        f"Return exactly {spec.slide_count} parts, in the order they will be told. For each "
        f"part write one line with its title (at most {budget.TITLE} characters, no numbering, "
        'naming its subject and never its role in the plan: no "Foundation:", "Payoff:" or '
        f'"Introduction"), then {POINTS_PER_PART} lines starting with "- ", each one specific '
        "idea, fact, number or example from the material that THIS part explains. Nothing "
        "else: no headings, no blank commentary.\n"
        "Rules:\n"
        "- Every point belongs to exactly one part. If two parts would explain the same idea, "
        "give it to the first and make the later one build on it instead.\n"
        "- The first part is the foundation: what the subject is and the problem it solves. "
        "The last part is the payoff: results, limits or what it means.\n"
        '- Points are concrete ("200 ms end-to-end latency on an L4 GPU"), never vague '
        '("discusses latency").\n'
        "- Cover only what the SOURCE and FACT material below actually says. Do not add "
        "topics from your own knowledge."
    )
    user = f"Session title: {spec.title}\n\nMaterial:\n\n{context.as_context()}"
    system = spec.adapt(system) + outline_shape(spec)
    plan = parse_plan(await complete(spec, system, user, "outline"), spec.slide_count)
    if not plan:
        raise Empty("outline")
    return plan


_LABEL_WORDS = ("part", "chapter", "section", "slide", "segment")
_LABEL_NUMS = (
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "i",
    "ii",
    "iii",
    "iv",
    "v",
    "vi",
    "vii",
    "viii",
)


def is_label(title: str) -> bool:
    """A title that is only a numbering: "PART ONE", "Part 2", "Section III"."""
    words = [w for w in (re.sub(r"^\W+|\W+$", "", w).lower() for w in title.split()) if w]
    return (
        len(words) == 2
        and words[0] in _LABEL_WORDS
        and (words[1] in _LABEL_NUMS or (words[1].isascii() and words[1].isdigit()))
    )


_SMALL = frozenset(
    {"the", "a", "an", "of", "and", "or", "to", "in", "on", "for", "with", "be", "is", "at"}
    | {"by", "from", "as"}
)


def fit_title(title: str) -> str:
    """A title cut to its budget, without a small word left hanging at the
    end ("…why text might not be the")."""
    t = budget.fit(title, budget.TITLE)
    if len(t) < len(title.strip()):
        while " " in t:
            head, _, last = t.rpartition(" ")
            if last.lower() not in _SMALL:
                break
            t = head.rstrip(",:;-—").rstrip()
    return t


def strip_slide_label(line: str) -> str:
    """An outline line without the "Slide 2:" or "2." a model puts in front
    of it.

    The prompt asks for no numbering and got "Slide 1: Process Isolation in
    Linux" anyway; that line is the slide's heading, so the label would be
    printed on the slide.
    """
    # "Part 1:" and "Chapter 1:" too: an audio overview's plan is asked for
    # parts, and a model numbers them the way it was asked.
    labelled = next(
        (
            line[len(w) :]
            for w in ("Slide", "slide", "Part", "part", "Chapter", "chapter")
            if line.startswith(w)
        ),
        None,
    )
    rest = (line if labelled is None else labelled).lstrip()
    digits = len(rest) - len(rest.lstrip("0123456789"))
    if digits == 0:
        return line
    tail = rest[digits:]
    # A bare number is a list marker only when punctuation follows it: "2. The
    # task_struct" is numbered, "64-bit address spaces" is a title.
    if labelled is None and not tail.startswith((".", ":", ")")):
        return line
    after = tail.lstrip(":.)-— ")
    return after or line


_BULLETS = ("- ", "* ", "• ", "– ")


def parse_plan(raw: str, count: int) -> list[PlanPart]:
    """Read a plan: a title line, then its `- ` points.

    A small model decorates — bold titles, "Slide 2:" labels, `*` bullets,
    numbered points — and a reply with no points at all is still a usable
    list of titles, which is exactly what the outline used to be. A point is
    told from a title by its bullet; a title is anything else that is not
    blank.
    """
    plan: list[PlanPart] = []
    for line in raw.splitlines():
        t = line.strip()
        if not t:
            continue
        bullet = next((t[len(b) :] for b in _BULLETS if t.startswith(b)), None)
        if bullet is not None:
            p = bullet.strip()
            if plan and p and len(plan[-1].points) < POINTS_PER_PART + 1:
                plan[-1].points.append(p)
            continue
        title = strip_slide_label(t.strip("*#_ "))
        title = title.lstrip("-*# ").strip()
        if title:
            plan.append(PlanPart(fit_title(title)))
    # A heading the model put over the plan ("Debate: Moshi") or a bare label
    # ("PART ONE") arrives as a part with no points. When other parts have
    # points those are the plan, and a part without is decoration: measured,
    # two such lines shifted every chapter title of a debate by two. A part
    # with no points is a heading when it comes before the first part that
    # has points, and padding when the plan is longer than asked; in the
    # middle of a plan of the right size it is a part whose points the model
    # forgot, and it stays.
    plan = [p for p in plan if not is_label(p.title)]
    first = next((i for i, p in enumerate(plan) if p.points), None)
    if first is not None:
        plan = plan[first:]
    if len(plan) > count:
        extra = len(plan) - count
        kept: list[PlanPart] = []
        for p in plan:
            if extra > 0 and not p.points:
                extra -= 1
                continue
            kept.append(p)
        plan = kept
    return plan[:count]


def plan_listing(plan: list[PlanPart], here: int | None) -> str:
    """The plan as the prompts show it, each part with the points it owns,
    and `here` marked when the reader is writing one of them."""
    out = ["The session, part by part, and the points each part alone explains:\n"]
    for i, p in enumerate(plan):
        mark = "  <- this slide" if i == here else ""
        out.append(f"  {i + 1}. {p.title}{mark}\n")
        out.extend(f"     - {point}\n" for point in p.points)
    return "".join(out)


# ── how the speakers talk ────────────────────────────────────────────────────

# What every slide is told about the session it sits in.
#
# Each slide is one model call, and until this existed each call saw only its
# own topic. Asked to narrate "Linux processes" with nothing else, a model
# does what a model does with a blank page: it opens a talk. A real five-slide
# session said "Welcome to our session" on four of its five slides and wrapped
# up on the fifth, and it played as five separate clips rather than one
# session. The greeting and the wrap-up belong to the bookends, written once;
# the slides are the middle, and they are told so.
CONTINUITY = (
    "This slide is one part of a single continuous session that is already under way. The "
    "listener has heard every slide before this one; the session's welcome and its closing are "
    "spoken separately, not by you. So never greet, welcome or introduce the session, never "
    'say what "today" or "this session" covers, and never sum up or wrap up. Pick up where '
    "the previous slide left off, without repeating it, and explain this slide's points — only "
    "this slide's: what other slides own is theirs to explain. If there is a next slide, you "
    "may end with a short statement that leads towards it, never a question. The first line "
    "may answer or build on the last thing said on the previous slide."
)


def roster(spec: ScriptSpec) -> str:
    """The speakers as the prompts list them: id, name, role."""
    return "\n".join(f"  {s.speaker_id} ({s.display_name}): {s.role}" for s in spec.speakers)


def format_rules(a: AudioSpec) -> str:
    """What an audio overview's format asks of the script, on top of the
    host dynamic in `dialogue`.

    NotebookLM's four formats, as its own descriptions put them (Deep Dive:
    "a lively conversation"; Brief: one host, under two minutes; Critique:
    "an expert review, offering constructive feedback"; Debate: "different
    perspectives"), written out the way the open implementations and
    listeners' complaints point. The stock phrases listeners tire of most
    are banned by name.
    """
    common = (
        'This is audio only. Nothing is ever on screen, so never say "as you can see", "on '
        'the slide" or "this chart", and say numbers the way a person says them aloud. Never '
        'say "deep dive", "let\'s dive in", "unpack", "buckle up" or "stay tuned", and do not '
        "let the hosts agree with each other more than once in six lines: agreeing all the time "
        "is the most common complaint about shows like this."
    )
    shape = {
        "deep_dive": "Format: Deep Dive. Two hosts in a lively conversation that makes the "
        "listener understand the material. Every chapter moves from a claim, to how it works, "
        "to a concrete example or analogy from the material, and somewhere in it the second "
        'host pushes back, adds a caveat or asks "but wait": real tension, not performed '
        "agreement.",
        "brief": "Format: Brief. One host, about two minutes, for a listener who wants the "
        "gist. Open on the single most important or surprising point, then the other key "
        "points with one concrete detail each, then a one-sentence takeaway. No banter, no "
        "greeting ritual, no padding.",
        "critique": "Format: Critique. Two hosts give the material an expert review, as if it "
        "were the listener's own work: the first host reviews, the second speaks for the author "
        "and asks what the fix would be. Strengths first, named specifically. Then the "
        "weaknesses, most important first, each with where it is in the material and what would "
        "fix it. Constructive and concrete, never vague praise or vague blame.",
        "debate": "Format: Debate. The two hosts take different positions on the question the "
        "material raises, each argued from what the material says, never a straw man. Each "
        "makes their case, they answer each other's strongest point, and each concedes "
        "something real. Nobody wins: the listener is left with the open question and what "
        "would settle it.",
    }[a.format]
    return f"{shape}\n{common}"


_BANNED = (
    'Never use these words or openers: "Certainly", "Absolutely", "Great question", '
    '"Fascinating", "That\'s interesting", "delve", "elaborate", "in summary", "in conclusion", '
    '"it\'s worth noting".'
)


def dialogue(spec: ScriptSpec) -> str:
    """How the speakers talk, said once and used by every call that writes
    speech.

    UNSPECIFIED BY SECTION 3: what two speakers do differently. A role of
    "narrator" next to "expert" told the model nothing about how a
    conversation moves, and it wrote an interview: one speaker asking "Can
    you elaborate on the practical implications of…?", the other answering in
    a paragraph, the same fact said by both. NotebookLM's two hosts are a
    curious host and an explaining one, and every question follows from the
    line before it; the rules below are that dynamic, written as plainly as
    the open reimplementations write it. The first speaker leads, which is
    the host's seat; the roles still say who they are.
    """
    if len(spec.speakers) < 2:
        return (
            "How it sounds:\n"
            '- Speak straight to the listener as "you", the way a good teacher explains one to '
            "one. You may raise the question the listener is probably thinking, and answer it in "
            "the same breath.\n"
            "- Short sentences. Concrete numbers and examples from the material, and an everyday "
            "analogy when it makes an idea click.\n"
            "- Say each idea once. Something already explained earlier in the session is "
            "referred to in a few words, never explained again.\n"
            f"- {_BANNED}"
        )
    host, guest = spec.speakers[0].display_name, spec.speakers[1].display_name
    # A debate and a critique are not a newcomer and an explainer: measured, a
    # Debate written with those roles had both hosts explaining the same side,
    # and the disagreement only arrived in the closing line. Their own roles
    # replace the first rule; every other rule still holds.
    f = spec.audio.format if spec.audio else None
    roles = None
    if f == "debate":
        roles = (
            f"- {host} argues one side of the question the material raises and {guest} argues "
            "the other, each from what the material says. They answer each other's strongest "
            "point directly, they keep their sides all the way through, and each concedes "
            "something real near the end. Neither is the listener's stand-in."
        )
    elif f == "critique":
        roles = (
            f"- {host} is the reviewer: names what works and what does not, specifically, with "
            f"where it is in the material. {guest} speaks for the author: explains the intent, "
            "pushes back where the criticism is unfair, and asks what the fix would be. "
            "Constructive, never vague praise or vague blame."
        )
    if roles is not None:
        return (
            "How the conversation sounds — two people talking, not an interview:\n"
            f"{roles}\n"
            "- Every question comes out of the line just before it, and is answered in the very "
            "next line. Nothing ends on an unanswered question.\n"
            "- Short turns, like real talk. Most lines are one or two sentences.\n"
            "- Never repeat or reword what the other speaker just said. Say each point once in "
            "the whole episode.\n"
            "- Do not start a line with the other speaker's name.\n"
            f"- {_BANNED}"
        )
    return (
        "How the conversation sounds — two people talking, not an interview:\n"
        f"- {host} leads and is the listener's stand-in: sharp but new to this. {host} asks what "
        'a smart newcomer would ask at that exact moment, reacts honestly ("Wait, so…", "Huh, '
        "that's faster than I'd have guessed\"), and now and then says an idea back in plain "
        f'words to check it ("So basically…"). {guest} explains, with the material\'s concrete '
        "numbers and examples, and an everyday analogy when one helps.\n"
        "- Every question comes out of the line just before it: something unclear, surprising, "
        'or a natural "so what?". Never a generic interview question such as "Can you '
        'elaborate on…", "How does X differ from Y?" or "What are the implications of…".\n'
        "- A question is answered in the very next line, and an answer never ends by asking the "
        "other speaker something back. Nothing ends on an unanswered question.\n"
        "- Short turns, like real talk. Most lines are one or two sentences; an explanation may "
        "take three. Some lines are just a quick reaction and the next thought.\n"
        "- Never repeat or reword what the other speaker just said. Say each idea once in the "
        'whole session: something already explained is referred to in a few words ("that inner '
        'monologue trick") and only what is new is added.\n'
        "- Do not start a line with the other speaker's name.\n"
        f"- {_BANNED}"
    )


# ── reading and cutting lines ────────────────────────────────────────────────


def named_to_ids(spec: ScriptSpec, text: str) -> str:
    """A reply with a speaker named by their display name rewritten to their
    id. The prompt lists each speaker as `host (Bella): narrator` and asks
    for the id, and a model writes "Bella:" anyway."""
    names = [
        (s.display_name.strip().lower(), s.speaker_id)
        for s in spec.speakers
        if s.display_name.strip()
    ]
    out: list[str] = []
    for line in text.split("\n"):
        t = line.lstrip()
        label, sep, rest = t.partition(":")
        if not sep:
            out.append(line)
            continue
        bare = label.strip().strip("*_").strip().lower()
        rest = rest.lstrip("*_")
        found = next((sid for n, sid in names if n == bare), None)
        out.append(line if found is None else f"{found}:{rest}")
    return "\n".join(out)


def lines_of(spec: ScriptSpec, text: str) -> list[ScriptedLine]:
    """A reply's spoken lines, with a speaker named by their display name
    read as that speaker.

    The parser knows ids and positions ("A:", "Speaker 1:") but not names, so
    a whole reply in names failed to parse: measured on a Brief, the
    whole-session reply AND both bookends came back unusable, and the episode
    ran 0.64 minutes of a 2-minute target. A name is as unambiguous as a
    position, so it is turned into the id before parsing.
    """
    return parse_lines(named_to_ids(spec, text), spec.speaker_ids())


def _lines_or_none(spec: ScriptSpec, text: str) -> list[ScriptedLine] | None:
    try:
        return lines_of(spec, text)
    except ScriptError:
        return None


_STOP = frozenset(
    [
        "the",
        "a",
        "an",
        "and",
        "or",
        "but",
        "of",
        "to",
        "in",
        "on",
        "for",
        "with",
        "by",
        "is",
        "are",
        "was",
        "it",
        "its",
        "it's",
        "this",
        "that",
        "these",
        "those",
        "as",
        "at",
        "from",
        "be",
        "can",
        "so",
        "than",
        "then",
        "also",
        "which",
        "while",
        "into",
        "without",
        "their",
    ]
)


def overlaps(a: str, b: str) -> bool:
    """Whether two lines say mostly the same thing: two in five or more of
    the content words of the shorter one are in the longer one. Half was too
    strict for a paraphrase — the measured pair shares seven of fifteen — and
    the test only ever runs on one speaker talking twice in a row, which a
    dialogue rarely does for a good reason."""

    def words(t: str) -> set[str]:
        return {
            w.lower() for w in re.split(r"[^\w']+|_", t) if len(w) > 2 and w.lower() not in _STOP
        }

    x, y = words(a), words(b)
    small, big = (x, y) if len(x) <= len(y) else (y, x)
    return len(small) >= 3 and sum(1 for w in small if w in big) * 5 >= len(small) * 2


def drop_restatements(lines: list[ScriptedLine]) -> list[ScriptedLine]:
    """Drop a line that says again what the same speaker said in the line
    before.

    A small script model often writes a point twice in a row, short and then
    long — measured on a real build, three slides of five had "Adam: Moshi is
    full-duplex and real-time." straight followed by "Adam: Moshi's
    full-duplex design means it can listen and speak simultaneously…". Heard
    aloud that is one person repeating themselves. The longer line is kept,
    because it is the one that carries the explanation.
    """
    out: list[ScriptedLine] = []
    for line in lines:
        if out and out[-1].speaker_id == line.speaker_id and overlaps(out[-1].text, line.text):
            if len(line.text) > len(out[-1].text):
                out[-1] = line
            continue
        out.append(line)
    return out


def drop_dangling_question(lines: list[ScriptedLine]) -> list[ScriptedLine]:
    """Drop the trailing lines that ask something nobody answers.

    A part is spoken and then the next one starts from its own script, so a
    question left at the end of a part is heard and never answered. Never
    empties a part.
    """
    lines = list(lines)
    while len(lines) > 1 and lines[-1].text.rstrip().endswith("?"):
        lines.pop()
    return lines


def _cut(lines: list[ScriptedLine], total: int) -> list[ScriptedLine]:
    """Whole sentences of each line until the budget is spent; once a line
    has been shortened or dropped, every line after it goes too."""
    spent = 0
    out: list[ScriptedLine] = []
    for line in lines:
        room = budget.room(spent, total)
        if room is None:
            break
        whole = len(line.text.strip()) <= room
        fitted = budget.whole_sentences(line.text, room)
        if fitted is None:
            break
        out.append(ScriptedLine(line.speaker_id, fitted))
        spent += len(fitted)
        if not whole:
            break
    return out


def fit_to(lines: list[ScriptedLine], total: int) -> list[ScriptedLine]:
    """Cut lines to a narration budget.

    The conversation is cut where it stops fitting, not thinned out. A line
    keeps its whole sentences or goes, and once one line has been shortened
    or dropped every line after it goes too: the next line was written to
    answer the one before it, and skipping a line turns a dialogue into non
    sequiturs. Then the part may not end on a question, because the line that
    answered it is the one that was cut — a real session ended slide 4 on
    "How does the 'Inner Monologue' method contribute to the." and moved on.
    """
    return drop_dangling_question(_cut(drop_restatements(lines), total))


_OPENERS = (
    "welcome",
    "hello",
    "hi everyone",
    "hi, everyone",
    "good morning",
    "good afternoon",
    "good evening",
    "today we",
    "today, we",
    "in this session",
    "in today's session",
    "in this presentation",
    "here's what you'll learn",
    "here is what you'll learn",
)


def drop_preamble(lines: list[ScriptedLine]) -> list[ScriptedLine]:
    """Drop the lines that open a talk rather than continue one.

    The net under `CONTINUITY`, for a model that greets regardless. Only the
    LEADING lines go, because a greeting comes first — a last slide that ends
    on "today we covered…" is closing, not opening, and keeps it. It matches
    the openings actually seen and never empties a slide: if every line is a
    greeting, the slide keeps them, because a slide that says "welcome" is a
    better outcome than a prep that fails over one.
    """
    lead = 0
    for line in lines:
        if not line.text.lstrip().lower().startswith(_OPENERS):
            break
        lead += 1
    return lines[lead:] if lead < len(lines) else lines


def same_stat(a: str, b: str) -> bool:
    """Two `Stat:` lines that show the same number. The number is what the
    eye catches; the caption under it is reworded from slide to slide."""

    def figure(s: str) -> str:
        return s.removeprefix("Stat:").split("—")[0].strip().lower()

    x, y = figure(a), figure(b)
    return bool(x) and x == y


@dataclass
class SlideDraft:
    """The spoken half and the printed half of one slide."""

    lines: list[ScriptedLine]
    # `Layout:` plus the element lines, the copy the slide model is given.
    # Empty when the model returned nothing usable, which leaves a title-only
    # slide.
    on_slide: list[str]


def shape_draft(
    lines: list[ScriptedLine], raw: str, elements: list[str], total: int, images: int | None
) -> SlideDraft:
    """A slide's parsed narration and copy, cut to budget.

    `raw` is the reply the layout is read from; `total` is the narration
    budget, which is larger on a slide that also carries the welcome.
    """
    # The layout is validated against the closed set and falls back rather
    # than being passed through: a name outside the set is worse than the
    # plain one, because the slide model then guesses the composition too.
    on_slide = [f"Layout: {layout_of(raw) or 'text only'}"]
    # Each `[image: …]` line is a picture to draw, so the limit is applied
    # here as well as asked for: a model told "at most one" sometimes writes
    # two.
    pictures = 0
    kept: list[str] = []
    for e in elements:
        if e.lstrip().startswith("[image:"):
            pictures += 1
            if images is not None and pictures > images:
                continue
        kept.append(e)
    for e in kept[: budget.SLIDE_ELEMENTS]:
        fitted = budget.fit(e, budget.ELEMENT)
        if fitted.strip():
            on_slide.append(fitted)
    # The budget is enforced on the way out, not requested and hoped for. A
    # line is dropped rather than shortened once the room left is too small
    # to say anything in — see `budget.room`.
    return SlideDraft(fit_to(lines, total), on_slide)


def slide_total(spec: ScriptSpec, ordinal: int) -> int:
    """A slide's narration budget: its own share, plus the welcome on the
    first slide, which rides on it. The close is not counted here: it is
    written apart and added after every cut, with its own budget."""
    return spec.slide_narration + (budget.BOOKEND_NARRATION if ordinal == 0 else 0)


# ── one slide on its own ─────────────────────────────────────────────────────


@dataclass
class Place:
    """A slide's place in the session: which one it is, what surrounds it,
    and how the slide before it ended."""

    ordinal: int
    plan: list[PlanPart]
    # The previous slide's last lines, as spoken, so this one continues rather
    # than restarts. Empty on the first slide.
    before: list[str]
    # The copy earlier slides already put on screen, so this one does not show
    # it again.
    shown: list[str]

    def here(self) -> PlanPart:
        return self.plan[self.ordinal]

    def describe(self) -> str:
        out = (
            f"{plan_listing(self.plan, self.ordinal)}\nThis is slide {self.ordinal + 1} of "
            f"{len(self.plan)}: {self.here().title}. Explain its points; the other slides' "
            "points are theirs."
        )
        if not self.before:
            out += "\n\nThe welcome has just been spoken; go straight into the topic."
        else:
            out += "\n\nThe previous slide ended with:\n" + "\n".join(self.before)
        if self.shown:
            out += (
                "\n\nAlready on earlier slides, so not to be shown again on this one — pick a "
                "different fact, number or point:\n" + "\n".join(self.shown)
            )
        if self.ordinal + 1 == len(self.plan):
            out += "\n\nThis is the last slide; the closing is spoken after it, not by you."
        return out


def _copy_rules(spec: ScriptSpec) -> str:
    return (
        f"Give one `Layout:` line naming one of: {', '.join(LAYOUTS)}.\n"
        f"Then at most {budget.SLIDE_ELEMENTS} lines, each one element, exact final wording:\n"
        "- Point: <a few words>\n"
        "- Subhead: <a short phrase>\n"
        "- Stat: <number> — <what it measures>\n"
        f"{spec.image_element()}"
    )


async def script_one(scope: Scope, spec: ScriptSpec, place: Place) -> SlideDraft:
    """Section 3 step 3: one call for one slide, grounded on that slide's own
    points."""
    # A long slide needs more to say: twice the passages once a slide is past
    # what one part of the whole-session call carries.
    k = TOP_K * 2 if spec.slide_narration > budget.WHOLE_SESSION_MAX_SLIDE else TOP_K
    context = await retrieve(scope, place.here().query(), k)
    system = (
        "You write the spoken narration for one slide of an explanatory session.\n"
        f"Speakers, by id:\n{roster(spec)}\n\n"
        f"{CONTINUITY}\n\n"
        f"{dialogue(spec)}\n\n"
        "Return only spoken lines. Start every line with one of those ids exactly as written, "
        "then a colon, then what they say — like this:\n"
        f"{example_id(spec)}: what they say\n"
        f"Use no other name and no other prefix. Every line must be at most {budget.LINE} "
        f"characters. Write between {spec.narration_target()} and {spec.slide_narration} "
        "characters of narration in all, explaining THIS slide's points with the examples and "
        "numbers in the material.\n"
        "Say only what the material below supports. Do not introduce facts from your own "
        "knowledge, and do not leave a placeholder of any kind.\n\n"
        "Then write the line `SLIDE:` on its own, and after it the copy that appears ON the "
        "slide. The slide is looked at while the narration is heard, so it carries the few "
        "words that anchor what is being said — never the spoken sentences again.\n"
        f"{_copy_rules(spec)}"
        f"Each element at most {budget.ELEMENT} characters. No sentences, no full stops at the "
        "end of a Point, no colours or fonts, and no placeholder — if the material does not "
        "support an element, leave it out."
    )
    user = f"{place.describe()}\n\nMaterial:\n\n{context.as_context()}"
    system = spec.adapt(system)
    user = spec.adapt_user(user)
    raw = await complete_partial(spec, system, user, "slide script")
    spoken, elements = split_slide_reply(raw)

    # Narration is the half that must survive.
    #
    # Asking one call for two things — spoken lines AND the slide's own copy —
    # is cheap when it works and is what keeps this to one model call per
    # slide. But the script model may be small, and a small model given two
    # jobs sometimes does only the second: observed returning `SLIDE:` and a
    # clean element block with no speaker-prefixed line in front of it, which
    # failed the whole prep. So the two jobs come apart on failure: one retry,
    # narration only. The retry also covers a reply whose speaker tags name
    # nobody: a formatting accident is not a reason to throw away an outline
    # that has already been paid for.
    first = _lines_or_none(spec, spoken)
    if not first:
        head, sep, _ = system.partition("\n\nThen write the line")
        narration_only = head if sep else system
        retry = await complete_partial(spec, narration_only, user, "slide script (narration only)")
        # The retry's reply is the one read: the first had no narration that
        # could be used, so there is nothing of it to prefer.
        spoken, _ = split_slide_reply(retry)
        # The elements from the first reply are still good — it was the
        # narration that was missing — so they are kept.
        if not elements:
            elements = split_slide_reply(raw)[1]
    # The narration is parsed from the spoken half only. Parsing the whole
    # reply would read `Point: …` as a line by a speaker called `Point`.
    lines = drop_preamble(lines_of(spec, spoken))
    if not lines:
        raise Empty("slide script")
    lines = await extend(spec, user, lines)
    return shape_draft(lines, raw, elements, spec.slide_narration, spec.images_per_slide)


async def extend(spec: ScriptSpec, material: str, lines: list[ScriptedLine]) -> list[ScriptedLine]:
    """Carry a slide's conversation on until it is close to its budget.

    A small model asked for 3,700 characters of one slide wrote about 2,000 on
    a real 20-minute build, so the session came out at nine minutes; a
    5-minute session written in one call came out at two. Asking harder in
    the one prompt does not fix that reliably; asking it to go on from where
    it stopped does, and each call costs a fraction of a cent. A slide
    already near its budget takes no call. A reply that adds nothing ends it,
    as does any error: the slide already has a script, and a shorter session
    is better than a failed one.
    """
    # Measured: a 3/4 goal left a 20-minute session at 16 minutes, two slides
    # stopping just under it. The budget's hard cut keeps an overshoot in check.
    goal = spec.slide_narration * 9 // 10
    lines = list(lines)
    for _ in range(EXTEND_CALLS):
        have = sum(len(line.text) for line in lines)
        if have >= goal:
            break
        more = max(spec.narration_target() - have, 600)
        so_far = "\n".join(f"{line.speaker_id}: {line.text}" for line in lines)
        # "Go deeper" with nothing to go deeper INTO is how a slide filled its
        # length by explaining the session's headline idea once more. The
        # continuation is pointed at this slide's own points, and at concrete
        # detail from the material, and told what belongs to other slides.
        system = (
            "You continue the spoken narration for one slide of an explanatory session.\n"
            f"Speakers, by id:\n{roster(spec)}\n\n"
            f"{dialogue(spec)}\n\n"
            "The conversation so far is below. Carry it on from its last line. Take up THIS "
            "slide's points that the conversation has not reached yet; once they are all "
            "covered, add what the material offers about them that has not been said: a "
            "concrete example, a number, a consequence, a limitation, or how one point leads to "
            "the next. Never re-explain what was already said, nothing that belongs to another "
            "slide, no greeting and no summing up. End on a statement, not a question.\n"
            "Return only the NEW spoken lines, each starting with a speaker id exactly as "
            "written, a colon, then what they say — like this:\n"
            f"{example_id(spec)}: what they say\n"
            f"Each line at most {budget.LINE} characters, about {more} characters in all. Say "
            "only what the material supports."
        )
        user = f"{material}\n\nThe conversation so far:\n{so_far}"
        try:
            raw = await complete_partial(
                spec, spec.adapt(system), spec.adapt_user(user), "slide script (continued)"
            )
        except ScriptError, AiError:
            # Any error ends the lengthening; the slide keeps what it has.
            log.info("a slide's narration was not lengthened", exc_info=True)
            break
        added = _lines_or_none(spec, split_slide_reply(raw)[0])
        if not added:
            break
        added = drop_preamble(added)
        if not added:
            break
        lines.extend(added)
    return lines


async def top_up(
    scope: Scope, spec: ScriptSpec, plan: list[PlanPart], ordinal: int, draft: SlideDraft
) -> SlideDraft:
    """Lengthen a slide from the whole-session script that came back short.

    That call writes every slide in one reply and a small model keeps each
    part brief: a 5-minute session came out at 2.1 minutes. The slide is
    carried on from its own material, as a slide written on its own is, then
    cut to its budget again.
    """
    have = sum(len(line.text) for line in draft.lines)
    if have >= spec.slide_narration * 9 // 10:
        return draft
    context = await retrieve(scope, plan[ordinal].query(), TOP_K)
    material = (
        f"{plan_listing(plan, ordinal)}\n\nMaterial:\n\n"
        f"{budget.fit(context.as_context(), MATERIAL_PER_PART)}"
    )
    lines = await extend(spec, material, draft.lines)
    return SlideDraft(fit_to(lines, slide_total(spec, ordinal)), draft.on_slide)


# ── the whole session in one piece ───────────────────────────────────────────


def speech_first(part: str, ids: list[str]) -> str:
    """A part with its spoken lines moved in front of its `SLIDE:` block.

    Writing the whole session, the model sometimes carries on talking after
    the slide copy — seen in a real reply, where part 1's second line came
    after its `Point:`. `split_slide_reply` reads everything after the marker
    as slide copy and drops a line it does not recognise, so that line would
    vanish without a trace. A line that opens with a speaker id is speech
    wherever it sits.
    """

    def is_speech(line: str) -> bool:
        label, sep, _ = line.partition(":")
        if not sep:
            return False
        label = label.strip().strip("*_")
        return any(i.lower() == label.lower() for i in ids)

    lines = part.splitlines()
    speech = [ln for ln in lines if is_speech(ln)]
    rest = [ln for ln in lines if not is_speech(ln)]
    return "\n".join(speech) + "\n" + "\n".join(rest)


def part_marker(line: str) -> int | None:
    """The number of a `=== PART n ===` marker, read loosely: `## Part 2`,
    `PART 2:`, `**Slide 2**` all count. A number must follow the word: that
    is what separates "Part 2" from the bare `SLIDE:` copy marker. Anything
    after the number is a title the model added, and is ignored."""
    t = line.strip().strip("=#*_-: []").lower()
    for word in ("part", "slide"):
        if t.startswith(word):
            rest = t[len(word) :].lstrip()
            digits = rest[: len(rest) - len(rest.lstrip("0123456789"))]
            return int(digits) if digits else None
    return None


def split_parts(raw: str, n: int) -> list[str | None]:
    """Cut a whole-session reply at its `=== PART n ===` markers. Text before
    the first marker is dropped; a part that appears twice keeps the
    first."""
    parts: list[str | None] = [None] * n
    current: int | None = None
    for line in raw.splitlines():
        k = part_marker(line)
        if k is not None:
            current = k - 1 if 1 <= k <= n and parts[k - 1] is None else None
            if current is not None:
                parts[current] = ""
            continue
        if current is not None:
            parts[current] = (parts[current] or "") + line + "\n"
    return parts


async def write_session(
    scope: Scope, spec: ScriptSpec, plan: list[PlanPart]
) -> list[SlideDraft | None]:
    """The whole session as one script, cut at the slide boundaries.

    This is how NotebookLM's overviews are made: the sources become a plan,
    the plan becomes ONE script in which the hosts carry a single thread from
    start to finish, and the visuals follow that script. Writing each slide
    in its own call is the opposite — five cold starts — and it sounded like
    it: a real session greeted the listener on four slides out of five. One
    call sees the whole arc, so the welcome is said once, each part picks up
    from the last, the speakers answer each other across a slide change, and
    the close comes at the end.

    Returns one entry per topic. None is a part the reply did not deliver or
    that could not be parsed — the caller writes that slide on its own rather
    than failing the prep, the same trade as everywhere else here.
    """
    ids = spec.speaker_ids()
    # Each part is grounded on its own points, and the material is labelled
    # by part so the model knows what belongs where: a part handed the whole
    # source explains the whole source, which is the repetition this replaced.
    material: list[str] = []
    for i, part in enumerate(plan):
        context = await retrieve(scope, part.query(), TOP_K)
        material.append(
            f"--- Material for part {i + 1} ({part.title}) ---\n"
            f"{budget.fit(context.as_context(), MATERIAL_PER_PART)}\n"
        )
    n = len(plan)
    system = (
        "You write the complete spoken script of a short explanatory session, from start to "
        "finish, in one piece.\n"
        f"Speakers, by id:\n{roster(spec)}\n\n"
        f"The session is shown as {n} slides, one per part, and the script is cut into the same "
        f"{n} parts. It is ONE continuous conversation, not {n} separate talks:\n"
        "- Part 1 opens with a hook: one or two sentences on why this matters to the listener "
        "— a surprising fact, a problem they will recognise, or a question the session answers "
        "— then goes straight into its points.\n"
        "- Every later part carries straight on from the one before it. No greeting, no "
        '"welcome", no "today we will", no re-introducing the subject, and no announcing the '
        'part ("let\'s continue", "now we explore", "moving on"): just say the next thing, the '
        "way a conversation moves on. A part may begin by building on the last line of the "
        "previous part, and may end with a statement that leads to the next.\n"
        "- Each part explains the points the plan gives it, and only those. A point owned by "
        "an earlier part has been explained already: refer back to it in a few words, never "
        "explain it again. A point owned by a later part is left for later.\n"
        f"- Part {n} ends on its last point. The closing is spoken after it, separately, so "
        "nothing is summed up or wrapped up anywhere in the script.\n\n"
        f"{dialogue(spec)}\n\n"
        "Format, exactly. For each part, in order:\n"
        "=== PART <number> ===\n"
        "then the spoken lines, each starting with a speaker id exactly as written, a colon, "
        "then what they say — like this:\n"
        f"{example_id(spec)}: what they say\n"
        f"Use no other name and no other prefix. Each line at most {budget.LINE} characters. "
        f"Each part has about {spec.narration_target()} characters of narration, and never "
        f"more than {spec.slide_narration}.\n"
        "Then the line `SLIDE:` on its own, and after it the copy that appears ON that part's "
        "slide — the few words that anchor what is being said, never the spoken sentences "
        f"again. One `Layout:` line naming one of: {', '.join(LAYOUTS)}. Then at most "
        f"{budget.SLIDE_ELEMENTS} lines, each one element, exact final wording:\n"
        "- Point: <a few words>\n"
        "- Subhead: <a short phrase>\n"
        "- Stat: <number> — <what it measures>\n"
        f"{spec.image_element()}"
        f"Each element at most {budget.ELEMENT} characters. No sentences, no full stops at the "
        "end of a Point, no colours or fonts.\n\n"
        "Say only what the material supports. Do not introduce facts from your own knowledge, "
        "and never leave a placeholder of any kind."
    )
    user = f"Session title: {spec.title}\n\n{plan_listing(plan, None)}\n{''.join(material)}"
    # Truncation keeps the parts before the cut; the ones after it are None
    # and are written one at a time.
    raw = await complete_partial(spec, spec.adapt(system), spec.adapt_user(user), "session script")
    out: list[SlideDraft | None] = []
    for i, part in enumerate(split_parts(raw, n)):
        if part is None:
            out.append(None)
            continue
        spoken, elements = split_slide_reply(speech_first(part, ids))
        lines = _lines_or_none(spec, spoken)
        if lines and i > 0:
            lines = drop_preamble(lines)
        if not lines:
            out.append(None)
            continue
        out.append(shape_draft(lines, part, elements, slide_total(spec, i), spec.images_per_slide))
    return out


# ── the bookends ─────────────────────────────────────────────────────────────

_INTRO = (
    "Write the OPENING of the session. Open with a hook — why this matters to the listener, or "
    "the question the session answers — then say in a breath what it covers. Do not cover the "
    'material itself — that is what the rest of the session is for. Do not say "in this '
    'presentation" or "today we will discuss"; just start.'
)
_OUTRO = (
    "Write the CLOSING of the session. In one or two sentences of ordinary conversation, say the "
    "one or two things most worth remembering — not a list of every slide. Then close warmly "
    "and briefly. Do not introduce anything new, do not thank the listener for watching, and do "
    "not invite questions — they could ask at any point and the session is over now."
)
# An audio overview closes the way its format ends: NotebookLM's deep dives
# end on a takeaway and a question to think about; a debate leaves its
# question open; a critique leaves the revisions in order.
_AUDIO_CLOSE = {
    "debate": "Write the CLOSING of the debate: each host says in one sentence what they would "
    "concede, then leave the listener with the open question and what would settle it. No "
    "winner. Close briefly.",
    "critique": "Write the CLOSING of the critique: the two or three revisions that matter "
    "most, in order of priority, in ordinary conversation. Close briefly and encouragingly.",
    "brief": "Write the CLOSING of the brief: the takeaway in one sentence. Nothing else.",
    "deep_dive": "Write the CLOSING of the episode: the one or two things worth remembering, "
    "said as conversation rather than a recap, then one question for the listener to think "
    "about, then a short sign-off. Do not introduce anything new.",
}


async def bookend(spec: ScriptSpec, plan: list[PlanPart], intro: bool) -> list[ScriptedLine]:
    """The opening or the closing, as spoken lines.

    A session that starts mid-explanation and stops mid-sentence sounds like
    a clip of something rather than a thing made for you. Both are written
    from the OUTLINE rather than from the source material, because they are
    about the shape of the session, not about its facts — and a bookend that
    invents a fact is worse than no bookend.

    They are lines on the first and last slides rather than slides of their
    own: the deck is built from the outline titles, and a title card saying
    "Introduction" is a slide nobody needs to look at.
    """
    if intro:
        job = _INTRO
    elif spec.audio is not None:
        job = _AUDIO_CLOSE[spec.audio.format]
    else:
        job = _OUTRO
    system = (
        "You write spoken narration for an explanatory session.\n"
        f"Speakers, by id:\n{roster(spec)}\n\n{job}\n\n"
        "Return only spoken lines. Start every line with one of those ids exactly as written, "
        "then a colon, then what they say — like this:\n"
        f"{example_id(spec)}: what they say\n"
        f"Use no other name and no other prefix. At most {BOOKEND_LINES} lines, each at most "
        f"{budget.LINE} characters. This is speech: no headings, no lists, no stage directions."
    )
    user = f"Session title: {spec.title}\n\n{plan_listing(plan, None)}"
    raw = await complete(
        spec, spec.adapt(system), spec.adapt_user(user), "intro" if intro else "outro"
    )
    # A bookend that cannot be parsed is dropped, not fatal. It is the frame
    # around the session, and a session without a frame is still the session.
    lines = _lines_or_none(spec, raw) or []
    # An audio overview's close carries more: a debate's needs both hosts to
    # concede something and the open question, which did not fit in one
    # line's budget and was cut mid-sentence.
    if spec.audio is not None:
        max_lines, total = BOOKEND_LINES + 2, 3 * budget.LINE
    else:
        max_lines, total = BOOKEND_LINES, budget.BOOKEND_NARRATION
    # Enforced on the way out, like every other budget here. Whole sentences
    # only, and a line that does not fit ends the bookend rather than being
    # chopped, the same rule as `fit_to`. Unlike `fit_to` a closing may end on
    # a question: one for the listener to think about is what a deep dive's
    # close is for.
    return _cut(lines[:max_lines], total)


async def _bookend_or_none(
    spec: ScriptSpec, plan: list[PlanPart], intro: bool
) -> list[ScriptedLine]:
    try:
        return await bookend(spec, plan, intro)
    except (ScriptError, AiError) as e:
        log.info("the %s was not written: %s", "opening" if intro else "close", e)
        return []


# ── the edit pass ────────────────────────────────────────────────────────────


def keeps_the_part(draft: list[ScriptedLine], edited: list[ScriptedLine]) -> bool:
    """Whether an edited part is still the part: not emptied, not a monologue
    where there was a conversation, and not cut by more than two fifths.
    Cutting is what the edit is FOR — a repeat removed is a shorter part — so
    the bound is loose; it is there to catch an editor that returned a
    summary."""

    def length(lines: list[ScriptedLine]) -> int:
        return sum(len(x.text) for x in lines)

    def voices(lines: list[ScriptedLine]) -> int:
        return len({x.speaker_id for x in lines})

    return (
        bool(edited)
        and length(edited) * 5 >= length(draft) * 3
        and voices(edited) >= min(voices(draft), 2)
    )


async def review(spec: ScriptSpec, plan: list[PlanPart], scripts: list[list[ScriptedLine]]) -> None:
    """The edit pass: every part read again by an editor who has heard
    everything before it, and fixed in place.

    NotebookLM's own pipeline, as its team described it, is outline, revised
    outline, script, critique, rewrite: the first draft is not what is
    spoken. A draft has the faults only a reader of the WHOLE script can see
    — the idea a later part explains again, the question nobody answered, the
    interview question that does not follow from anything — and the writer
    of one part cannot see them. So each part is sent back with the script
    before it, in order, so the edit of part 3 reads the already-edited parts
    1 and 2.

    An edit is a chance to make a part worse, so it is only taken when it is
    plainly the same part: it parses, it names the same speakers, and it did
    not lose more than two fifths of its length. Otherwise the draft stands,
    and any failure leaves the draft as it was — the pass improves a script,
    it never costs one.
    """
    n = len(scripts)

    def speak(lines: list[ScriptedLine]) -> str:
        return "\n".join(f"{x.speaker_id}: {x.text}" for x in lines)

    for i in range(n):
        if not scripts[i]:
            continue
        earlier = "\n".join(
            f"--- part {k + 1} ---\n{speak(lines)}" for k, lines in enumerate(scripts[:i])
        )
        # The most recent part matters most, so a long session keeps the tail.
        earlier = earlier[max(len(earlier) - REVIEW_CONTEXT, 0) :]
        greet = (
            "a welcome longer than two sentences"
            if i == 0
            else "a greeting or welcome — the session is already under way"
        )
        system = (
            "You are the editor of a spoken explanatory session. You get the plan, what has "
            f"already been said, and the draft of part {i + 1} of {n}. Return part {i + 1}, "
            "fixed.\n"
            f"Speakers, by id:\n{roster(spec)}\n\n"
            "Fix only these faults, and keep everything else as it is:\n"
            "1. An idea already explained earlier is explained again: cut the repeat, or "
            "replace it with a brief reference back and keep only what is new.\n"
            '2. A generic interview question ("Can you elaborate on…", "How does X differ from '
            'Y?", "What are the implications of…") that does not come out of the line before '
            "it: rewrite it as a real reaction to that line, or cut it.\n"
            "3. A question not answered by the very next line, or a part that ends on a "
            "question: answer it from what the draft says, or cut it.\n"
            "4. A sentence that is cut off or unfinished: finish it or cut it.\n"
            "5. A line that repeats or rewords the line before it: cut it.\n"
            '6. Stock phrases: "Certainly", "Absolutely", "Great question", "Fascinating", '
            f'"delve", "elaborate", "in summary", a line starting with the other speaker\'s '
            f"name, or {greet}.\n"
            "Do not add facts, do not change who says what unless a fix needs it, and keep the "
            "part about as long as the draft: a fix that cuts a sentence makes room for the next "
            "sentence of explanation, it does not shorten the part.\n"
            f"Return only the spoken lines of part {i + 1}, each starting with a speaker id "
            "exactly as written, a colon, then what they say — like this:\n"
            f"{example_id(spec)}: what they say\n"
            f"Each line at most {budget.LINE} characters."
        )
        user = (
            f"{plan_listing(plan, i)}\nAlready said, in order:\n"
            f"{earlier or '(nothing: this is the first part)'}\n\n"
            f"DRAFT OF PART {i + 1}:\n{speak(scripts[i])}"
        )
        try:
            raw = await complete_partial(
                spec, spec.adapt(system), spec.adapt_user(user), "script edit"
            )
        except (ScriptError, AiError) as e:
            log.info("the edit of part %d failed (%s); the draft stands", i + 1, e)
            continue
        edited = _lines_or_none(spec, split_slide_reply(raw)[0])
        if edited is None:
            continue
        if i > 0:
            edited = drop_preamble(edited)
        if not keeps_the_part(scripts[i], edited):
            log.info("edit of part %d not taken; the draft stands", i + 1)
            continue
        scripts[i] = fit_to(edited, slide_total(spec, i))


def slug(topic: str, ordinal: int) -> str:
    """A snake_case slide name, safe as a file name, so the name chosen here
    is the name the deck ends up with rather than a placeholder to reconcile
    later."""
    body = "".join(c.lower() if c.isascii() and c.isalnum() else "_" for c in topic)
    words = [w for w in body.split("_") if w][:5]
    return "_".join(words) if words else f"slide_{ordinal}"


# ── the whole script ─────────────────────────────────────────────────────────


async def generate_script(scope: Scope, spec: ScriptSpec) -> list[Part]:
    """Build the output's parts. Nothing here synthesises: `audio_path` and
    `duration_ms` stay absent."""
    plan = await outline(scope, spec)

    # One script for the whole session. A reply that fails outright is not
    # fatal: every slide is then written on its own, as below. Said in the
    # worker's log: a silent fallback is how the first deploy of this failed
    # without anyone being able to say why.
    one_piece = spec.slide_narration <= budget.WHOLE_SESSION_MAX_SLIDE
    whole: list[SlideDraft | None] = []
    if not one_piece:
        log.info(
            "%d characters a slide is a long session; writing each slide on its own",
            spec.slide_narration,
        )
    else:
        try:
            whole = await write_session(scope, spec, plan)
        except (ScriptError, AiError) as e:
            log.info("whole-session script failed (%s); writing each slide on its own", e)
    whole = (whole + [None] * len(plan))[: len(plan)]
    missing = [str(i + 1) for i, p in enumerate(whole) if p is None]
    if missing:
        log.info("slide(s) %s written on their own", ", ".join(missing))

    parts: list[Part] = []
    scripts: list[list[ScriptedLine]] = []
    # Closes written apart from the whole-session reply, added after the edit
    # pass so neither the cut nor the editor can take them.
    closes: dict[int, list[ScriptedLine]] = {}
    before: list[str] = []
    shown: list[str] = []
    for ordinal, topic in enumerate(plan):
        last = ordinal + 1 == len(plan)
        draft = whole[ordinal]
        if draft is not None:
            # The close is written on its own and added after the cut, never
            # asked of the whole-session reply: it came last in that reply, so
            # it was the first thing the budget cut, and a real build ended on
            # a question about latency with no goodbye.
            if last:
                closes[ordinal] = await _bookend_or_none(spec, plan, intro=False)
            draft = await top_up(scope, spec, plan, ordinal, draft)
        else:
            # The fallback: this slide alone, told where it sits and how the
            # previous one ended, with the welcome or the close written
            # separately when this slide is the one that carries it.
            draft = await script_one(scope, spec, Place(ordinal, plan, list(before), list(shown)))
            if ordinal == 0:
                draft.lines = await _bookend_or_none(spec, plan, intro=True) + draft.lines
            if last:
                # Kept out of the edit pass with the whole-session close: the
                # editor trims a closing question, and a deep dive's close
                # ends on one on purpose.
                closes[ordinal] = await _bookend_or_none(spec, plan, intro=False)
        before = [f"{x.speaker_id}: {x.text}" for x in draft.lines[-CARRIED_LINES:]]
        # The backstop under the prompt: a stat already shown is dropped, and
        # every remaining element is remembered for the slides after this one.
        # Written one slide at a time, a small model reached for the same
        # quotable number on all five slides of a real build.
        on_slide = [
            e
            for e in draft.on_slide
            if not (e.startswith("Stat:") and any(same_stat(s, e) for s in shown))
        ]
        shown.extend(e for e in on_slide if not e.startswith("Layout:"))
        scripts.append(draft.lines)
        parts.append(
            Part(
                slide=slug(topic.title, ordinal),
                ordinal=ordinal,
                # The outline's own words: the slide's heading.
                title=topic.title,
                on_slide=on_slide,
                lines=[],
                collection=spec.deck_collection,
                presentation=spec.deck_presentation,
            )
        )

    await review(spec, plan, scripts)
    for ordinal, close in closes.items():
        scripts[ordinal] = scripts[ordinal] + close

    for part, scripted in zip(parts, scripts, strict=True):
        part.lines = [
            Line(f"s{part.ordinal}l{n}", x.speaker_id, n, x.text) for n, x in enumerate(scripted)
        ]
    return parts
