"""The model's part of a motion video: one HTML page that animates the whole
narration (`docs/motion-style.md`).

The page is one call, unlike a whiteboard's scenes: an animation is one
composition whose scenes flow into each other, and its beats are timed
against the same numbered narration a whiteboard's writer gets. The plan of
scenes is the whiteboard's (`whiteboard/write.py`'s `plan`): grouping the
lines into scenes of 10 to 25 seconds is the same job for either style.

The answer is held to the same ladder a whiteboard scene is
(`whiteboard/write.py`): its form, then its declared words against what was
said, with exactly what is wrong sent back, up to `ATTEMPTS` times, then one
try by a stronger model, then the plain page (`render.fallback`). What needs
a recorded frame — the vision check and the layout lint — is checked in
`render.py`, after the first record, because a motion page has no still to
look at until it has run.
"""

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from opennotebook.ai import client
from opennotebook.ai.errors import AiError
from opennotebook.build import timeline as tl
from opennotebook.build.motion import ground, reply
from opennotebook.build.motion.lint import page_problems
from opennotebook.build.motion.scene import Node, Page, resolve
from opennotebook.build.whiteboard import render as board_render
from opennotebook.build.whiteboard import write as board
from opennotebook.build.whiteboard.scene import Plan
from opennotebook.domain.sessions import Part
from opennotebook.script import retrieval
from opennotebook.script.retrieval import Scope

log = logging.getLogger(__name__)

# The most a page may be answered with: one document with the whole
# narration around it. The deck's own slide batches ask for as much.
MAX_TOKENS = 24_000
# The page's first answer and up to two repairs (spec 4.7: two rounds pay
# off, more rarely do), then the stronger model once.
ATTEMPTS = 3
# Each prompt's first sentence, which says what is asked; the tests' fake
# model answers by it.
MARK = "You write one page for an animated explainer video."

SYSTEM = f"""{MARK}
You write ONE complete HTML page that is an animated explainer video: the studio records it in
a browser at 1920x1080, frame by frame, against the narration, and merges the voice. Your page
is the whole video's picture — every scene, its motion, its typography.

WHAT THE STUDIO GIVES YOUR PAGE (read these in your own script; fetch NOTHING — the recording
has no network):
- window.__VIDEO: {{title, duration_ms, width, height}}.
- window.__SCENES: the plan's scenes, each with start_ms, end_ms and the manifest you declare
  for it: [{{i, title, start_ms, end_ms, elements: [...], claims: [...]}}].
- window.__CHAPTERS: the narration's chapters: [{{i, title, start_ms, end_ms}}].
- window.__BEATS: EVERY word of the narration and when it is said:
  [{{line, part, w, text, start_ms, end_ms}}]. `w` counts from 0 within its line, the same
  numbering your prompt's [0][1][2] markers use.
- window.__Prelude.onFrame(fn): call it ONCE, with fn(t) — t is the video's time in ms — and
  draw your entire animation from t. It is called once per recorded frame, at exact times.
  window.__Prelude.now() is t outside it. NEVER use requestAnimationFrame, setInterval, CSS
  animations or transitions, or a clock of your own: every frame must be a pure function of t,
  or the recording will not match the words.
- window.__done: set it to true ONLY once t has passed duration_ms (in your last frame).

HOW THE PAGE IS BUILT (the contract):
- A scene's window is [start_ms, end_ms) from __SCENES. Its contents are fully visible while
  start_ms <= t < end_ms, and hidden (display:none or opacity 0) outside it — unless an element
  deliberately carries over (the closing title, a line held on screen).
- EVERY word you write on screen carries data-mn="<kind>:<id>" on the DOM node showing it, so
  the studio can measure where it landed. Kinds: text, card, figure, bar, icon, line, arrow
  (mark every one of these), and panel, mark (background layers: do not mark). ids are unique
  across the page.
- Everything appears ON ITS BEAT: an element's beat is {{line, word}}; that word's start_ms in
  __BEATS is when it starts appearing (within 150 ms, never before). Reveal, wipe, count up —
  your choice, but the timing is the word's.
- Position in %, vh/vw or px inside 1920x1080. Every label sits inside the frame with room;
  NO TWO text labels overlap AT ANY INSTANT; hidden things take no space (display:none, not
  opacity 0, for what must not collide before it appears).
- ONE idea at a time: signal what is being said (a rule, a wash, a pulse) and let the rest rest.
  Nothing decorative — if the narration does not mention it, do not draw it.

YOUR ANSWER — one JSON object, no prose around it and no second object:
{{"html": "<!doctype html> ... </html>", "elements": [...], "claims": [...]}}

Keep it tight: a few hundred lines. Build the scenes from a small set of helpers you define
once; never repeat a rule a variable would do. The reply must hold the whole page.

- "html": ONE document from <!doctype html> to </html>, with <head> and your <script> before
  </body>, closed with </script>. No <script src>, no <link>, no @import, no url() except
  data: or url(#id) — nothing is fetched. No emoji (they record as boxes), no <img>.
- "elements": one per thing you write on screen or that carries meaning, each
  {{"id": "...", "kind": "...", "text": "<1-3 words, from the narration>", "about": "<what it
  is, in a phrase — not written anywhere>", "beat": {{"line": "<line id>", "word": n}}, "scene":
  <scene index, or omit>, "value": <a real number, figure/bar only>, "glyph": "<thing, icon
  only>", "from": "<id>", "to": "<id>" (line/arrow)}}]. 1 to 40 in each scene's window.
- "claims": what a scene asserts as a short sentence with the passages behind it:
  {{"text": "...", "passages": ["p1"], "scene": <index>}}. A claim must be said by the
  narration or the passages: never invent a fact, a number or a relation.

RULES:
- Every word in "text" comes from that scene's narration or the passages; a label the
  narration never says is a fault. Scene titles are headings and are fine.
- Numbers only as the narration says them.
- Every millisecond from 0 to duration_ms is drawn: no blank stretch, and the last frame is
  not black.
- A short scene's window shows less. The narration sets the pace; never run past it.
"""

REPAIR_TAIL = """

Your last answer had these problems; fix every one and answer with the whole page again, as
one JSON object:
- """


@dataclass
class Built:
    """The page as it will be recorded, and how far the first tries got."""

    page: Page | None
    prompt: str
    # The model the accepted page came from: who is asked for a repair.
    model: str = ""
    # What the whole narration and its passages say: a repair is held to it
    # as the first answer was (`validate`).
    said: ground.Said | None = None
    first_try: bool = False
    plain: bool = False
    # Written again by the stronger model after the repairs failed.
    escalated: bool = False
    # The declared words and how many the narration or the sources say;
    # the declared claims and how many they support.
    texts: int = 0
    grounded: int = 0
    claims: int = 0
    supported: int = 0
    # Whether the vision check could be made for this page's scenes.
    checked: bool = False


async def _complete(model: str, prompt: str) -> str:
    done = await client.ai().complete(
        model,
        [{"role": "system", "content": SYSTEM}, {"role": "user", "content": prompt}],
        max_tokens=MAX_TOKENS,
    )
    return done.text


def scenes_of(plan_: Plan) -> dict[str, int]:
    """Each narration line to the index of the plan's scene covering it."""
    return {lid: i for i, sc in enumerate(plan_.scenes) for lid in sc.lines}


def windows_of(plan_: Plan, timing: tl.Timeline) -> list[tuple[float, float]]:
    """When each of the plan's scenes is on screen: the whiteboard's rule —
    a scene opens where the last one shut, and the last runs to the end."""
    return board.windows(plan_, timing)


def said_of(parts: list[Part], passages: list[str]) -> ground.Said:
    """Everything a page may write: the words and numbers of the whole
    narration and its passages. One `Said` for the page, because a motion
    page's labels may cross a scene's lines (the plan's own windows are
    where scenes begin, not where words may be used)."""
    return ground.Said.of([ln.text for p in parts for ln in p.lines] + list(passages))


def validate(page: Page, plan_: Plan, timing: tl.Timeline, said: ground.Said) -> list[str]:
    """Everything wrong with a written page before anything is recorded:
    beats on lines the plan does not cover or past their words, duplicate
    ids, joins to ids that are not there, claims for scenes that do not
    exist, and every word it declares it will write, unsaid
    (`lint.page_problems`)."""
    lines = {sp.line_id: len(sp.words) for sp in timing.lines}
    nodes, claims = resolve(page, scenes_of(plan_))
    found: list[str] = []
    for n in nodes:
        if n.beat.line not in lines:
            found.append(f"{n.id} is drawn on line {n.beat.line!r}, which is not in the plan")
        elif n.beat.word >= lines[n.beat.line]:
            found.append(
                f"{n.id} is drawn on word {n.beat.word} of {n.beat.line}, which has "
                f"{lines[n.beat.line]}"
            )
    found += page_problems(page.model_copy(update={"elements": nodes}), said)
    for c in claims:
        if c.scene is not None and not 0 <= int(c.scene) < len(plan_.scenes):
            found.append(f"the claim {c.text!r} belongs to a scene that does not exist")
    return found


def counts(nodes: list[Node], said: ground.Said) -> tuple[int, int]:
    """How many words the page declares it writes, and how many were said.
    Anything with text is grounded, whatever its kind: a mark that writes
    words is a label too."""
    return ground.counts([n.text for n in nodes if n.text.strip()], said)


def user_prompt(
    plan_: Plan, timing: tl.Timeline, windows: list[tuple[float, float]], passages: list[str]
) -> str:
    """What the page's writer is asked: the plan's scenes with their
    windows, and the narration inside each numbered word by word exactly as
    a whiteboard's writer sees it, so the beats count the same words."""
    spans = {sp.line_id: sp for sp in timing.lines}
    rows: list[str] = []
    for i, sc in enumerate(plan_.scenes):
        start, end = windows[i]
        rows.append(
            f"## Scene {i + 1}: {sc.title or sc.brief}  "
            f"[{start / 1000:.1f}s – {end / 1000:.1f}s], {(end - start) / 1000:.1f} s"
        )
        if sc.brief and sc.brief != sc.title:
            rows.append(f"Shows: {sc.brief}")
        if sc.concepts:
            rows.append("Concepts: " + ", ".join(sc.concepts))
        for lid in sc.lines:
            sp = spans.get(lid)
            if sp is not None:
                numbered = " ".join(f"[{i}]{w}" for i, w in enumerate(tl.tokens_of(sp.text)))
                rows.append(f"{lid}: {numbered}")
        rows.append("")
    rows.append(f"The video lasts {timing.total_ms / 1000:.1f} seconds.")
    if passages:
        rows += ["", 'Source passages the video is made from (cite them by id in "claims"):]']
        rows += [f"p{i + 1}: {p}" for i, p in enumerate(passages)]
    return "\n".join(rows)


async def passages_of(scope: Scope, plan_: Plan, parts: list[Part]) -> list[str]:
    """The source passages every scene's claims may cite: what retrieval
    finds for each scene's lines and its title, as a whiteboard's scenes'
    own passages (`whiteboard/render._passages`), gathered once for the
    page. Empty when the read fails: the page is then held to its
    narration alone."""
    by_id = {ln.line_id: ln.text for p in parts for ln in p.lines}
    out: list[str] = []
    seen: set[str] = set()
    for sc in plan_.scenes:
        query = " ".join([sc.title, sc.brief, *(by_id.get(lid, "") for lid in sc.lines)])
        try:
            got = await retrieval.retrieve(
                scope, query[: board_render.QUERY_CHARS], board_render.PASSAGES
            )
        except Exception:
            log.warning("passages for a motion scene could not be read", exc_info=True)
            continue
        found = got.passages[: board_render.PASSAGES * 2]
        found += [f"{a} ({q})" for q, a in got.qa[: board_render.PASSAGES]]
        for p in found:
            if p not in seen:
                seen.add(p)
                out.append(p)
    return out


def narration_of(plan_: Plan, parts: list[Part]) -> list[list[str]]:
    """Each scene's narration, for the vision check: its lines' own text,
    in order."""
    by_id = {ln.line_id: ln.text for p in parts for ln in p.lines}
    return [[by_id[lid] for lid in sc.lines if lid in by_id] for sc in plan_.scenes]


async def write_page(
    model: str,
    plan_: Plan,
    timing: tl.Timeline,
    windows: list[tuple[float, float]],
    passages: list[str],
    said: ground.Said,
    *,
    escalate_model: str = "",
) -> Built:
    """The page written and held to its form and its words, with exactly
    what is wrong sent back up to `ATTEMPTS` times, then once by
    `escalate_model`; never usable means the plain page (`page is None`,
    `plain`)."""
    prompt = user_prompt(plan_, timing, windows, passages)
    ask = prompt
    problems: list[str] = []
    writers = [model] * ATTEMPTS
    if escalate_model and escalate_model != model:
        writers.append(escalate_model)
    for attempt, writer in enumerate(writers):
        page: Page | None = None
        try:
            page = Page.model_validate(reply.json_of(await _complete(writer, ask)))
            problems = validate(page, plan_, timing, said)
        except AiError:
            if attempt < ATTEMPTS:
                raise
            # The stronger model is out of reach: the plain page stands in.
            break
        except (ValueError, ValidationError) as e:
            page, problems = None, [f"the answer is not a valid page: {str(e)[:400]}"]
        if not problems and page is not None:
            nodes, claims = resolve(page, scenes_of(plan_))
            texts, grounded = counts(nodes, said)
            return Built(
                page.model_copy(update={"elements": nodes, "claims": claims}),
                prompt,
                model=writer,
                said=said,
                first_try=attempt == 0,
                escalated=attempt >= ATTEMPTS,
                texts=texts,
                grounded=grounded,
                claims=len(claims),
                supported=len(claims),
                checked=True,
            )
        log.info("the motion page needs another try: %s", "; ".join(problems[:8]))
        ask = prompt + REPAIR_TAIL + "\n- ".join(problems[:40])
    return Built(None, prompt, plain=True, checked=True)


async def rewrite(
    model: str, built: Built, plan_: Plan, timing: tl.Timeline, faults: list[str]
) -> Page | None:
    """One repair round after the first record: the faults found in the
    recorded frames and where the page put things, sent back with the
    original prompt. None when the answer does not read as a page."""
    if not faults:
        return built.page
    try:
        page = Page.model_validate(
            reply.json_of(await _complete(model, built.prompt + REPAIR_TAIL + "\n- ".join(faults)))
        )
        problems = validate(page, plan_, timing, built.said or ground.Said.of([]))
        if problems:
            log.info("the motion page's repair is still wrong: %s", "; ".join(problems[:8]))
            return None
    except (AiError, ValueError, ValidationError) as e:
        log.info("the motion page's repair did not read: %s", e)
        return None
    nodes, claims = resolve(page, scenes_of(plan_))
    return page.model_copy(update={"elements": nodes, "claims": claims})
