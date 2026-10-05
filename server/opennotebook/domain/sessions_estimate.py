"""What a build will cost, before it runs. A port of
`opennotebook_server/src/estimate.rs` and `estimate_live.rs`.

A build pays for model calls at the AI endpoint (OpenRouter by default), all
made through `ai.client`. Mapped from the code on 2026-10-03:

- **Q&A extraction**, while reading the sources: calls per source per
  dimension (4), on `openai/gpt-4o-mini`: one for a source up to 32,000
  characters, else one per window of it (`memory.qa.windows_for`, at most
  six), together reading the whole source. The search index is full-text by
  default and free; embeddings, when an embedding model is set, are not
  estimated.
- **The script**: the outline, the narration script and its edit pass on the
  script model, and the slides, written as HTML a batch of four per call on
  the slide model (`build.slides`). An audio overview has no slides.
- **Web research**, only when a prep is asked to research a topic.
- Narration is the local speech server and free.

What is exact and what is not. Every INPUT is measured: the real sources, the
real settings, the live prices from the endpoint's `/models` catalogue. OUTPUTS
are not knowable before the models run, so they are a low / typical / high
range, calibrated where a measurement exists (each constant below says which)
and bounded by the build's own limits where none does. The dialog says so
rather than printing a precise-looking number.

Tokens are counted as characters / 4, the usual English average. A prep
records what it really spent on its row (`sessions.spent_usd`), which is the
number to check these constants against.
"""

import math
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opennotebook.ai import client
from opennotebook.ai.prices import Price
from opennotebook.db.models import Source
from opennotebook.domain import settings as st
from opennotebook.domain.sessions import Shape
from opennotebook.memory import qa
from opennotebook.script import budget

# Characters per token, for English prose.
CHARS_PER_TOKEN = 4.0

# Q&A extraction: the four dimensions a build asks for, each one call per
# window of each source.
QA_DIMENSIONS = len(qa.DIMENSIONS)
QA_MODEL = qa.DEFAULT_QA_MODEL
# What every call sends besides the source: the system prompt, the JSON schema
# and the request's header (dimension, focus, budget), measured from the prompt
# source.
QA_PROMPT_TOKENS = 650


@dataclass(frozen=True)
class Range:
    """A low / typical / high count."""

    low: int
    typical: int
    high: int

    @classmethod
    def exact(cls, n: int) -> Range:
        return cls(n, n, n)


# Up to ten pairs per source and dimension, each with a one-to-three-sentence
# answer and a short quote. The call is capped at 3000; a full ten pairs is
# about 1000, an empty dimension almost nothing. A source read in several
# windows asks each for its share, at least four, so it writes somewhat more
# in all: see `qa_out_share`. Not measured yet: the calls are recorded in the
# spend ledger, which is where a measurement would come from.
QA_OUT = Range(250, 1000, 1800)
# A small source cannot fill ten answers: a call writes at most twice what it
# read, its answers restating the source with a question each, and never less
# than this, a pair or two. Reasoned, not measured.
QA_OUT_FLOOR = 200

# The outline reads up to 24 excerpts of ~900 characters and 12 Q&A pairs.
OUTLINE_MATERIAL_CHARS = 24 * 900
OUTLINE_QA_TOKENS = 12 * 80
OUTLINE_PROMPT_TOKENS = 300
# One title of at most 60 characters per slide, and the three points it owns.
OUTLINE_OUT_PER_SLIDE = 80

# The whole-session script: each part gets up to 8 excerpts and 4 Q&A pairs,
# capped at `MATERIAL_PER_PART` (12,000) characters.
SCRIPT_PART_CHARS = 8 * 900 + 4 * 320
MATERIAL_PER_PART = 12_000
SCRIPT_PROMPT_TOKENS = 900
# Per part, on top of its narration: up to 4 slide elements of 72 characters.
# The opening and closing add 320 each.
SLIDE_COPY_CHARS = 4 * 72
SCRIPT_OUT_CHARS_BOOKENDS = 2 * 320
# The narration that comes back, as a share of its budget. Measured on the
# four 5-minute decks of prep jobs 00ky, 00l4, 00l7 and 00l8 (892 characters a
# slide budgeted, after lengthening): 3,114 to 5,013 characters of 4,460, so
# 0.70 to 1.12; three 5-minute audio overviews (00m3, 00m5, 00m7) came to 0.89
# to 1.05 of theirs. Those are after lengthening, so priced on the first pass
# as well this errs high.
NARRATION_SHARE = (0.7, 0.9, 1.15)

# The slide writer (`build.slides`): one call per batch of `PER_CALL` (4)
# slides, so a 5-slide deck is a batch of 4 and a batch of 1. Each call
# carries the style kit's instructions, recipe and example: 5,015 to 5,565
# characters across the eight kits, measured from `slides.prompt` on
# 2026-10-03, plus the style's brief and the batch header.
SLIDES_PER_CALL = 4
SLIDE_PROMPT_CHARS = 5_400
# Per slide in a call: its title, copy and a narration excerpt of at most 500
# characters. Prep jobs 00ks to 00m1 (eight 5-slide decks, two calls each)
# sent 11k to 14k characters in all, which leaves 200 to 800 a slide after the
# two prompts.
SLIDE_COPY_IN_CHARS = 700
# Characters written per slide: a whole HTML document with its figure in SVG.
# The same eight decks, on Haiku 4.5, wrote 9k to 13k characters for five
# slides: 1,800 to 2,600 a slide.
SLIDE_OUT_CHARS = Range(1_500, 2_200, 3_000)

# A prep asked to research a topic: one planning call, up to five web
# searches on the search model (Sonar, about half a cent each), and one report
# call on the notes model over up to twelve pages of 8,000 characters, about
# 24k tokens in and up to 6k out. Reasoned from today's prices, not measured;
# every one of those calls is recorded in the spend ledger, so a build's real
# figure is on its row. Low is the quick depth, high a notes model priced like
# Sonnet. USD, low / typical / high.
RESEARCH_USD = (0.02, 0.03, 0.20)

GROUP_SOURCES = "Reading your sources"
GROUP_SCRIPT = "Writing the script"
GROUP_SLIDES = "Designing the slides"
GROUP_VOICE = "Recording the narration"
GROUP_RESEARCH = "Researching the web"

SCRIPT_VIA = "opennotebook · OpenRouter"


@dataclass(frozen=True)
class Inputs:
    """Everything the estimate is computed from."""

    # Characters in each source.
    source_chars: list[int]
    slides: int
    speakers: int
    script_model: str
    # The model that writes the slides.
    slide_model: str
    # Narration characters per slide, from the session length.
    slide_narration: int
    minutes: int
    # An audio overview: no slides are designed, so that group is left out.
    audio: bool = False
    # The prep researches a topic on the web first.
    research: bool = False


@dataclass
class Line:
    group: str
    step: str
    detail: str
    # Empty for a step that calls no model.
    model: str
    # The service that makes the call and how it is paid.
    via: str
    calls: Range
    input_tokens: int
    output_tokens: Range
    cost: tuple[float, float, float]
    free: bool = False
    # Set when the model has no price in the catalogue: the line is counted as
    # zero and says so, rather than silently.
    unpriced: bool = False
    price: Price | None = None


@dataclass
class Estimate:
    lines: list[Line]
    # Low, typical, high, USD.
    total: tuple[float, float, float]
    assumptions: list[str] = field(default_factory=list[str])
    source_chars: int = 0


def grouped(n: int) -> str:
    """44072 → "44,072"."""
    return f"{n:,}"


def tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


def qa_out_share(full: int, windows: int) -> int:
    """What one of `windows` calls on a source writes, from what a single
    call reading the whole source writes (`full`): the pairs it is asked for,
    a tenth of the source's ten each but never fewer than four, at the same
    length each."""
    if windows <= 1:
        return full
    return full * max(-(-10 // windows), 4) // 10


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def totals_line(
    prices: dict[str, Price],
    group: str,
    step: str,
    detail: str,
    model: str,
    via: str,
    calls: Range,
    input_: Range,
    output: Range,
) -> Line:
    """A line priced from its token totals, for a step whose calls differ in
    size: the slide batches, the Q&A over sources of different lengths."""
    price = prices.get(model)
    if price is None:
        cost = (0.0, 0.0, 0.0)
    else:
        cost = (
            price.cost(input_.low, output.low),
            price.cost(input_.typical, output.typical),
            price.cost(input_.high, output.high),
        )
    return Line(
        group=group,
        step=step,
        detail=detail,
        model=model,
        via=via,
        calls=calls,
        # A step that usually does not run shows what it reads when it does,
        # not "0 in".
        input_tokens=input_.high if calls.typical == 0 else input_.typical,
        output_tokens=output,
        cost=cost,
        unpriced=price is None,
        price=price,
    )


def line(
    prices: dict[str, Price],
    group: str,
    step: str,
    detail: str,
    model: str,
    via: str,
    calls: Range,
    input_per_call: int,
    out_per_call: Range,
) -> Line:
    """A priced line. `calls` and `out` vary together: the low total is the
    fewest calls writing the least, the high the most writing the most."""
    return totals_line(
        prices,
        group,
        step,
        detail,
        model,
        via,
        calls,
        Range(
            input_per_call * calls.low, input_per_call * calls.typical, input_per_call * calls.high
        ),
        Range(
            out_per_call.low * calls.low,
            out_per_call.typical * calls.typical,
            out_per_call.high * calls.high,
        ),
    )


def free(group: str, step: str, detail: str, via: str, calls: int) -> Line:
    return Line(
        group, step, detail, "", via, Range.exact(calls), 0, Range.exact(0), (0, 0, 0), True
    )


def slide_design(n: int) -> tuple[Range, Range, Range]:
    """The slide writer's calls and token totals for `n` slides: batches of
    `SLIDES_PER_CALL`, each carrying the whole prompt once and its own
    slides' copy. Low and typical ask each batch once; high asks every batch
    twice."""
    calls = -(-n // SLIDES_PER_CALL)
    input_ = tokens(calls * SLIDE_PROMPT_CHARS + n * SLIDE_COPY_IN_CHARS)
    return (
        Range(calls, calls, 2 * calls),
        Range(input_, input_, 2 * input_),
        Range(
            tokens(n * SLIDE_OUT_CHARS.low),
            tokens(n * SLIDE_OUT_CHARS.typical),
            2 * tokens(n * SLIDE_OUT_CHARS.high),
        ),
    )


def estimate(inp: Inputs, prices: dict[str, Price]) -> Estimate:
    n = max(inp.slides, 1)
    files = len(inp.source_chars)
    total_chars = sum(inp.source_chars)
    lines: list[Line] = []
    # An audio overview's parts are chapters, and the whole of it an overview.
    part, parts, whole = (
        ("chapter", "chapters", "overview")
        if inp.audio
        else (
            "slide",
            "slides",
            "session",
        )
    )

    # ── web research ─────────────────────────────────────────────────────────
    if inp.research:
        lines.append(
            Line(
                GROUP_RESEARCH,
                "Web research",
                "Plan searches, run up to 5, read up to 12 pages, write a cited report",
                "",
                "OpenRouter",
                Range(5, 7, 7),
                0,
                Range.exact(0),
                RESEARCH_USD,
            )
        )

    # ── reading the sources ──────────────────────────────────────────────────
    # A call per window of each source per dimension, the windows together
    # reading the whole source, so input and output are summed source by
    # source rather than averaged. Characters stand in for the bytes the
    # windows are cut by; for English text they are the same. A source over
    # about 570,000 characters is sampled rather than read whole, so its input
    # is overstated here, which errs on the safe side of the spending limit.
    qa_calls = sum(qa.windows_for(c) for c in inp.source_chars) * QA_DIMENSIONS
    qa_in = sum(
        (tokens(c) + QA_PROMPT_TOKENS * qa.windows_for(c)) * QA_DIMENSIONS for c in inp.source_chars
    )
    out = [0, 0, 0]
    for c in inp.source_chars:
        w = qa.windows_for(c)
        # Each window writes its share of the pairs, and no more than twice
        # what it read.
        cap = max(2 * tokens(c // w), QA_OUT_FLOOR)
        for i, full in enumerate((QA_OUT.low, QA_OUT.typical, QA_OUT.high)):
            out[i] += min(qa_out_share(full, w), cap) * w * QA_DIMENSIONS
    lines.append(
        totals_line(
            prices,
            GROUP_SOURCES,
            "Question & answer extraction",
            f"{files} source{_plural(files)} × {QA_DIMENSIONS} kinds of question, each reading "
            "the whole source (a long one in parts); these calls are included in recorded spend",
            QA_MODEL,
            "opennotebook · OpenRouter",
            Range.exact(qa_calls),
            Range.exact(qa_in),
            Range(out[0], out[1], out[2]),
        )
    )
    lines.append(
        free(
            GROUP_SOURCES,
            "Search index",
            "every source split into passages and indexed for full-text search",
            "Postgres · free",
            files,
        )
    )

    # ── the script ───────────────────────────────────────────────────────────
    outline_in = (
        OUTLINE_PROMPT_TOKENS + tokens(min(total_chars, OUTLINE_MATERIAL_CHARS)) + OUTLINE_QA_TOKENS
    )
    lines.append(
        line(
            prices,
            GROUP_SCRIPT,
            "Outline",
            f"{n} {part} titles from the sources, each with the points it alone explains",
            inp.script_model,
            SCRIPT_VIA,
            Range.exact(1),
            outline_in,
            Range.exact(OUTLINE_OUT_PER_SLIDE * n),
        )
    )
    # An audio overview's script writes no on-screen copy.
    slide_out_chars = inp.slide_narration + (0 if inp.audio else SLIDE_COPY_CHARS)
    per_slide = inp.slide_narration > budget.WHOLE_SESSION_MAX_SLIDE
    # A part's material is what retrieval finds, so a small collection gives
    # less than the cap.
    part_chars = min(SCRIPT_PART_CHARS, total_chars + 4 * 320, MATERIAL_PER_PART)
    # A slide written on its own is given twice the material.
    material = min(2 * part_chars, MATERIAL_PER_PART) if per_slide else part_chars

    def share(chars: int) -> Range:
        t = tokens(chars)
        return Range(
            int(t * NARRATION_SHARE[0]), int(t * NARRATION_SHARE[1]), int(t * NARRATION_SHARE[2])
        )

    voices = f"{inp.speakers} voice{_plural(inp.speakers)}"
    if per_slide:
        # A long session is written one slide per call, plus the welcome and
        # the close on their own.
        lines.append(
            line(
                prices,
                GROUP_SCRIPT,
                "Narration script",
                f"one {part} per call, about {inp.minutes} minutes in all, {voices}",
                inp.script_model,
                SCRIPT_VIA,
                Range.exact(n),
                SCRIPT_PROMPT_TOKENS + tokens(material),
                share(slide_out_chars),
            )
        )
        lines.append(
            line(
                prices,
                GROUP_SCRIPT,
                "Welcome and close",
                "two short calls, written from the outline",
                inp.script_model,
                SCRIPT_VIA,
                Range.exact(2),
                OUTLINE_PROMPT_TOKENS + OUTLINE_OUT_PER_SLIDE * n,
                Range.exact(tokens(320)),
            )
        )
    else:
        with_copy = "" if inp.audio else ", with each slide's text"
        lines.append(
            line(
                prices,
                GROUP_SCRIPT,
                "Narration script",
                f"the whole {whole} in one piece, {voices}{with_copy}",
                inp.script_model,
                SCRIPT_VIA,
                Range.exact(1),
                SCRIPT_PROMPT_TOKENS + tokens(part_chars) * n,
                share(slide_out_chars * n + SCRIPT_OUT_CHARS_BOOKENDS),
            )
        )
        # Only when the one-piece script misses a part: that slide is
        # rewritten on its own. Usually none; at worst every slide, once more.
        lines.append(
            line(
                prices,
                GROUP_SCRIPT,
                f"Per-{part} rewrite, if needed",
                f"only for a {part} the one-piece script missed",
                inp.script_model,
                SCRIPT_VIA,
                Range(0, 0, n),
                SCRIPT_PROMPT_TOKENS + tokens(part_chars),
                Range.exact(tokens(slide_out_chars)),
            )
        )
        # The one-piece script no longer writes the close: it came last and
        # was the first thing the budget cut, so it is its own short call.
        lines.append(
            line(
                prices,
                GROUP_SCRIPT,
                "Close",
                "one short call, written from the outline",
                inp.script_model,
                SCRIPT_VIA,
                Range.exact(1),
                OUTLINE_PROMPT_TOKENS + OUTLINE_OUT_PER_SLIDE * n,
                Range.exact(tokens(320)),
            )
        )

    # A slide that comes back short is carried on in up to three more calls,
    # each re-reading its material and what was said so far. How many run is
    # not logged; one a slide is the typical guess.
    lines.append(
        line(
            prices,
            GROUP_SCRIPT,
            f"Lengthening short {parts}",
            f"only for a {part} whose narration came back short of its length",
            inp.script_model,
            SCRIPT_VIA,
            Range(0, n, 3 * n),
            SCRIPT_PROMPT_TOKENS + tokens(material + slide_out_chars),
            Range.exact(tokens(slide_out_chars // 2)),
        )
    )

    # Every part read again with the script before it, and fixed: one call a
    # part, each reading what was said so far (capped at 12,000 characters)
    # and writing the part back.
    earlier = min(slide_out_chars * n // 2, MATERIAL_PER_PART)
    lines.append(
        line(
            prices,
            GROUP_SCRIPT,
            "Editing the script",
            "each part re-read with everything said before it, to cut repeats and unanswered "
            "questions",
            inp.script_model,
            SCRIPT_VIA,
            Range.exact(n),
            SCRIPT_PROMPT_TOKENS
            + OUTLINE_OUT_PER_SLIDE * n
            + tokens(earlier + inp.slide_narration),
            share(inp.slide_narration),
        )
    )

    # ── the slides ───────────────────────────────────────────────────────────
    # An audio overview has none: its parts are chapters, heard and not seen.
    if not inp.audio:
        calls, input_, output = slide_design(n)
        lines.append(
            free(
                GROUP_SLIDES,
                "Style kit",
                "fonts, colours and textures, added to every slide by the studio",
                "opennotebook · no call",
                0,
            )
        )
        lines.append(
            totals_line(
                prices,
                GROUP_SLIDES,
                "Slide design",
                f"{n} slide{_plural(n)} in {calls.typical} call{_plural(calls.typical)} of up to "
                f"{SLIDES_PER_CALL}, each figure drawn in SVG; a batch that comes back short is "
                "asked again",
                inp.slide_model,
                SCRIPT_VIA,
                calls,
                input_,
                output,
            )
        )

    # ── the voice ────────────────────────────────────────────────────────────
    # Lines run about 250 characters; never fewer than the short-slide count.
    spoken = max(n * inp.slide_narration // 250, n * 3) + 2
    lines.append(
        free(
            GROUP_VOICE,
            "Narration audio",
            f"about {spoken} spoken lines",
            "local voice · free",
            spoken,
        )
    )

    total = (
        sum(ln.cost[0] for ln in lines),
        sum(ln.cost[1] for ln in lines),
        sum(ln.cost[2] for ln in lines),
    )
    drawn = "" if inp.audio else f"slides written by {inp.slide_model}, "
    assumptions = [
        f"Measured: {files} source{_plural(files)} ({grouped(total_chars)} characters), {n} "
        f"{'chapters' if inp.audio else 'slides'}, about {inp.minutes} minutes, {voices}, "
        f"{drawn}today's prices.",
        "Estimated: how much each model writes. The range covers it; the narration lengths are "
        "calibrated on past builds."
        if inp.audio
        else "Estimated: how much each model writes, and whether a batch of slides has to be "
        "asked twice. The range covers both; the slide and narration lengths are calibrated on "
        "past builds.",
        "Tokens are counted as characters ÷ 4.",
        "A call that fails is retried up to 3 times, and a retry is billed again; that is not "
        "included.",
        "Web research is a range reasoned from its calls and today's prices; what it really cost "
        "is recorded with the build."
        if inp.research
        else "Research you ran in the chat was billed then, and is not part of the build.",
    ]
    if any(ln.unpriced for ln in lines):
        assumptions.append(
            'A model with no price in the catalog is counted as $0 and marked "no price"; the '
            "real cost is higher."
        )
    return Estimate(lines, total, assumptions, total_chars)


# ── the live half ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Live:
    """An estimate with what it was computed from, as the route answers it."""

    estimate: Estimate
    inputs: Inputs
    style: str
    priced_at: str
    limit_usd: float

    @property
    def over_limit(self) -> bool:
        return self.limit_usd > 0 and self.estimate.total[2] > self.limit_usd


class NoPrices(Exception):
    """The endpoint's catalogue could not be read or lists no prices."""

    sentence = (
        "The AI provider's price list could not be read, so the cost cannot be estimated. "
        "Try again in a minute."
    )


async def source_chars(s: AsyncSession, owner: uuid.UUID, cid: uuid.UUID) -> list[int]:
    """Characters in each of a collection's sources, in the order added."""
    rows = await s.scalars(
        select(Source.chars)
        .where(Source.owner_id == owner, Source.collection_id == cid)
        .order_by(Source.created_at)
    )
    return list(rows)


async def compute(
    s: AsyncSession,
    owner: uuid.UUID,
    chars: list[int],
    sh: Shape,
    style: str,
    research: bool,
    prices: dict[str, Price] | None = None,
) -> Live:
    """Gather the real inputs and price them. Nothing here calls a model:
    the prices come from the endpoint's catalogue, which needs no key, or
    are `prices` when the caller read them already (outside its lock)."""
    v = await st.values(s, owner)
    # An audio overview runs as long as its format says, not the session
    # length setting.
    minutes = sh.audio.minutes() if sh.audio else st.session_minutes(v[st.SESSION_MINUTES_KEY])
    slides = max(sh.slides, 1)
    inputs = Inputs(
        source_chars=chars,
        slides=slides,
        speakers=max(sh.speakers, 1),
        script_model=v[st.SCRIPT_MODEL_KEY],
        slide_model=v[st.SLIDE_MODEL_KEY],
        slide_narration=budget.slide_narration(minutes, slides),
        minutes=minutes,
        audio=sh.audio is not None,
        research=research,
    )
    if prices is None:
        prices = await client.ai().catalogue.prices()
    if not prices:
        # A local server (Ollama, LM Studio) lists models without prices.
        raise NoPrices
    limit = st.parse_limit(v[st.MAX_BUILD_USD_KEY]) or 0.0
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Live(estimate(inputs, prices), inputs, style, stamp, limit)


def cent_up(x: float) -> float:
    """Up to the next whole cent, so an amount over a limit never prints as
    the limit itself ($0.2525 against $0.25 reads $0.26). The slack keeps
    float noise ($0.81 stored as 0.8100000001) from adding a cent."""
    return math.ceil(x * 100.0 - 1e-6) / 100.0


def over_limit_message(high: float, limit: float, audio: bool) -> str:
    """Why a build was refused, with what would bring it under the limit."""
    fewer = "a shorter length" if audio else "fewer slides, a shorter length"
    return (
        f"This could cost up to ${cent_up(high):.2f}, over your ${limit:.2f} limit. "
        f"Use {fewer}, or raise the limit in Settings › Costs & limits."
    )


async def refuse_over_limit(
    s: AsyncSession,
    owner: uuid.UUID,
    chars: list[int],
    sh: Shape,
    research: bool,
    prices: dict[str, Price] | None = None,
) -> str | None:
    """Why a build whose HIGH estimate is over the person's spending limit is
    refused, or None.

    The high end, not the typical one: the limit is a promise about the most
    a build costs, and a build that usually fits but sometimes does not would
    break it. None when there is no limit, and when the estimate itself
    cannot be made — a price catalogue that is down is not a reason to stop a
    build the person asked for."""
    if st.parse_limit(await st.value(s, owner, st.MAX_BUILD_USD_KEY)) is None:
        return None
    try:
        live = await compute(s, owner, chars, sh, "", research, prices)
    except NoPrices:
        return None
    if live.over_limit:
        return over_limit_message(live.estimate.total[2], live.limit_usd, sh.audio is not None)
    return None
