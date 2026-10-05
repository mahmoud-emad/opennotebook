# ruff: noqa: E501
"""The deck: one HTML document per slide, written by the slide model and
drawn with the style's kit (`build.kits`). A port of
`opennotebook_build/src/slides.rs`.

This replaced an earlier slide service. That service designed each slide with
a model of its own, an image model per picture and a visual check, and
forbade inline SVG; the studio sent it the slide copy and waited, often past
its 90-second window, for decks whose colours came from prose it interpreted.
Side by side in Slide Lab, one call that writes the slides as HTML with the
figures drawn in SVG, and a kit that supplies every colour, font and texture,
came out more on-style, faster and cheaper.

The content is not the model's to choose: each slide's title and copy come
from the script, so the slide shows what the narration says. The model lays
that copy out with the kit's classes and draws one figure that explains it.

Slides are written a few per call, calls in parallel. A batch that comes back
short or broken is asked once more; a slide still missing after that gets a
plain slide built here from its copy, so a session never fails over a figure.

Files: `decks/<sid>/studio/<slide>.html` on the files volume, the layout the
Rust studio wrote and the player reads.
"""

import asyncio
import html as html_lib
import logging
from dataclasses import dataclass

from opennotebook import storage
from opennotebook.ai import client
from opennotebook.ai.errors import AiError
from opennotebook.build import kits
from opennotebook.build.clean import ascii_lower, clean
from opennotebook.build.errors import FilesNotWritten
from opennotebook.domain.sessions import Part

log = logging.getLogger(__name__)

# Slides per model call. Four keeps a reply near 10k characters, well inside
# the output limit, and lets a 12-slide deck go out as three calls at once.
PER_CALL = 4

# Output ceiling per call. Measured on Haiku 4.5 (prep jobs 00ks to 00m1,
# October 2026): 9k to 13k characters for five slides, so about 2k a slide and
# 8k for a full batch of four. This leaves room for a style that draws far
# more.
MAX_TOKENS = 16_000

# The presentation name every studio-written deck lives under.
PRESENTATION = "studio"


def deck_dir(sid: object) -> str:
    """The output's deck directory on the files volume."""
    return f"decks/{sid}"


def slide_path(sid: object, slide: str) -> str:
    """The file a slide of this deck is written to."""
    return f"{deck_dir(sid)}/{PRESENTATION}/{slide}.html"


@dataclass(frozen=True)
class DeckPlan:
    """What the deck is drawn in and where it is written."""

    title: str
    kit: kits.Kit
    style_label: str
    style_brief: str
    # The slide model, e.g. `anthropic/claude-haiku-4.5`.
    model: str
    # A sentence fixing the output language, or empty for English.
    language_rule: str
    # The output's id: its deck directory, and the deck's collection.
    sid: str


@dataclass
class DeckOutcome:
    # Slide names, one per slide, in the order given.
    written: list[str]
    # Slides the model did not deliver, drawn as plain slides instead.
    fallbacks: int
    # Characters the model was sent and wrote, for the cost log.
    chars_in: int
    chars_out: int


def complete_docs(text: str) -> list[str]:
    """Every complete `<!doctype …>` … `</html>` document in a reply, in
    order."""
    lower = ascii_lower(text)
    out: list[str] = []
    at = 0
    while (s := lower.find("<!doctype", at)) >= 0:
        e = lower.find("</html>", s)
        if e < 0:
            break
        e += len("</html>")
        out.append(text[s:e])
        at = e
    return out


def usable(doc: str) -> bool:
    """A slide worth keeping. The cleaner takes care of colours and
    positioning, so what is left to judge is whether the slide still says
    what it is about: it has its title, as the kit's title."""
    lower = ascii_lower(clean(doc))
    return "k-title" in lower and "<body" in lower


def prompt(plan: DeckPlan, chunk: list[Part], start: int, total: int) -> tuple[str, str]:
    """The system and user prompts for one batch."""
    k = plan.kit
    language = (
        f"{plan.language_rule} Keep the class names and HTML exactly as specified."
        if plan.language_rule
        else "Write any extra short labels in English."
    )
    system = f"""You design presentation slides as HTML for a narrated explainer session. You are given each slide's exact copy; lay it out and draw its figure.

STYLE: {plan.style_label}. {plan.style_brief}

THE STYLE KIT — this deck has a fixed style kit that is added to every slide after you write it. It sets the fonts, every colour, the background and texture, spacing, and the look of every component and illustration class below. So:
- Write NO CSS for colours, fonts, font sizes, backgrounds, borders, shadows, radii or filters, and no <link> or @import. If a slide needs a layout tweak, a short <style> with only grid/flex/width/height/gap/margin/order is allowed.
- Use only these classes. The <body> is already a column with an 80px margin and a 34px gap; put blocks directly in it.
  k-kicker: small label above a title.   k-title (on <h1>): the slide title.   k-sub: one-line subtitle.
  k-split: two columns, text left and figure right (add k-fig-wide for a larger figure). k-row: a row of 2–4 equal cards. k-stack: a vertical group. k-center: centred block.
  k-points (on <ul>): up to 4 short points.   k-card: a card; an <h3> inside is its heading.   k-stat (with k-card): a stat, holding <div class="k-num"> and <div class="k-cap">.   k-hl (on <span>): highlight one key phrase.
  k-figure (on <figure>): holds the slide's one inline <svg viewBox="…">; a <figcaption class="k-figcap"> inside it is a caption.
  k-bento: a grid of tiles instead of k-split; inside it k-tile elements (k-tile-wide spans two columns, k-tile-tall two rows), each with an <h3> and a <p>, or a <div class="k-num">, or a small inline <svg>.
  k-steps: a row of numbered steps instead of k-split; inside it 3–5 k-step elements, each with <div class="k-n">1</div>, an optional small inline <svg>, an <h3> and a <p>. The kit draws the arrows.
  Inside an SVG use only these classes, never fill/stroke/style attributes of your own: k-ink (a drawn line), k-f0 (paper/white), k-f1 (accent), k-f2 / k-f3 / k-f4 (the style's illustration colours), k-shade (shadow or depth), k-lab (on <text>: a short label, at most 3 words).

ILLUSTRATION RECIPE FOR THIS STYLE (follow it exactly; the kit's filters do the texture):
{k.recipe}
A small example of the technique (draw your own subject, larger and more detailed, 10–25 elements):
{k.example}

SLIDE STRUCTURE FOR THIS STYLE (its signature layout — use it):
{kits.structure(k)}

LAYOUT: {k.layout_rule} Vary the composition from slide to slide.

RULES:
1. Each slide is ONE complete HTML document from <!doctype html> to </html>. No <script>, no images, no emoji, no url() except url(#k-…) inside an SVG.
2. Nothing may extend past 1920×1080: keep to at most 4 points or 3 cards or 7 tiles, short lines, and one figure.
3. Text never sits on an SVG. Labels inside an SVG use k-lab: at most 12 characters, centred with text-anchor="middle", inside the viewBox with room to spare, at most 6 per figure.
4. Use the given copy: the title as the k-title, the points, stats and subheads as given (you may shorten them). A slide needs substance: if it has fewer than three points, add short points taken from its narration excerpt (only what the narration says, never outside facts). The figure explains what the slide is about and is not decoration. Use k-num only for a real number or short code from the copy or narration (a count, a percentage, a PID); never for a word like "Varies" — without one, use an <h3> instead.
5. One flat page on the kit's background: the kicker, the title and the content are direct children of <body>, never wrapped in an extra container. No position:absolute or fixed, no layers or overlays, no background shapes, no full-slide rectangles, no gradients and no opacity: everything is drawn at full strength, and a figure sits in the layout beside or below the text, never behind it.
6. {language}

OUTPUT FORMAT — the slides in the order given, nothing else, no commentary, no Markdown fences:
<!-- SLIDE n -->
<!doctype html>
...
</html>"""
    user = [
        f"Session: {plan.title}\nThis batch is slides {start + 1}–{start + len(chunk)} of {total}.\n"
    ]
    for i, s in enumerate(chunk):
        n = start + i + 1
        said = " ".join(line.text for line in s.lines)[:500]
        user.append(f"\n=== SLIDE {n} ===\nTitle: {s.title}\n")
        if n == 1:
            user.append(
                "This slide opens the session: give it a title-slide treatment (a large title "
                "with a k-kicker and a k-sub, and a figure).\n"
            )
        user.extend(f"{e}\n" for e in s.on_slide if not e.startswith("[image:"))
        user.append(f"Narration excerpt: {said}\n")
    return system, "".join(user)


def plain_slide(plan: DeckPlan, s: Part) -> str:
    """A slide built here from its copy, for one the model did not deliver:
    the title, its points, its stat. No figure, but on-style and readable."""

    def esc(t: str) -> str:
        return html_lib.escape(t, quote=False)

    points, stat, sub = "", "", ""
    for e in s.on_slide:
        if e.startswith("Point:"):
            points += f"<li>{esc(e.removeprefix('Point:').strip())}</li>"
        elif e.startswith("Stat:"):
            v = e.removeprefix("Stat:")
            num, _, cap = v.partition("—")
            stat = (
                f'<div class="k-card k-stat"><div class="k-num">{esc(num.strip())}</div>'
                f'<div class="k-cap">{esc(cap.strip())}</div></div>'
            )
        elif e.startswith("Subhead:"):
            sub = f'<p class="k-sub">{esc(e.removeprefix("Subhead:").strip())}</p>'
    body = (
        ""
        if not points and not stat
        else f'<div class="k-stack"><ul class="k-points">{points}</ul>{stat}</div>'
    )
    t = esc(s.title)
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><title>{t}</title></head><body>'
        f'<div class="k-kicker">{esc(plan.title)}</div><h1 class="k-title">{t}</h1>{sub}{body}'
        "</body></html>"
    )


async def _complete(model: str, system: str, user: str) -> str:
    done = await client.ai().complete(
        model,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=MAX_TOKENS,
    )
    return done.text


@dataclass
class _Batch:
    # One per slide asked for, in order; None where the model never delivered
    # a usable one.
    docs: list[str | None]
    chars_in: int = 0
    chars_out: int = 0


async def write_batch(plan: DeckPlan, chunk: list[Part], start: int, total: int) -> _Batch:
    """One batch: asked once, and once more if any slide came back missing or
    unusable. A slide that was usable the first time keeps that version."""
    system, user = prompt(plan, chunk, start, total)
    span = f"{start + 1}–{start + len(chunk)}"
    batch = _Batch([None] * len(chunk))
    for attempt in (1, 2):
        batch.chars_in += len(system) + len(user)
        try:
            text = await _complete(plan.model, system, user)
        except AiError as e:
            log.info("slides %s failed on %s (%s) (attempt %d)", span, plan.model, e, attempt)
            continue
        batch.chars_out += len(text)
        for i, doc in enumerate(complete_docs(text)[: len(chunk)]):
            if batch.docs[i] is not None:
                continue
            if usable(doc):
                batch.docs[i] = doc
            else:
                log.info(
                    "slide %d came back without its title (attempt %d)", start + i + 1, attempt
                )
        have = sum(1 for d in batch.docs if d is not None)
        if have == len(chunk):
            return batch
        log.info(
            "slides %s came back with %d of %d usable slides (attempt %d)",
            span,
            have,
            len(chunk),
            attempt,
        )
    return batch


async def write_deck(plan: DeckPlan, parts: list[Part]) -> DeckOutcome:
    """Write every slide of the output, in parallel batches. The names come
    back in slide order (by ordinal)."""
    order = sorted(parts, key=lambda p: p.ordinal)
    batches = [order[i : i + PER_CALL] for i in range(0, len(order), PER_CALL)]
    written = await asyncio.gather(
        *(write_batch(plan, chunk, i * PER_CALL, len(order)) for i, chunk in enumerate(batches))
    )
    out = DeckOutcome([], 0, 0, 0)
    for chunk, batch in zip(batches, written, strict=True):
        out.chars_in += batch.chars_in
        out.chars_out += batch.chars_out
        for i, slide in enumerate(chunk):
            doc = batch.docs[i]
            if doc is None:
                out.fallbacks += 1
                doc = plain_slide(plan, slide)
            try:
                storage.put(slide_path(plan.sid, slide.slide), kits.apply(doc, plan.kit).encode())
            except OSError as e:
                raise FilesNotWritten(f"slide {slide.slide}: {e}") from e
            out.written.append(slide.slide)
    return out
