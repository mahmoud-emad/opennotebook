"""The model's part of a whiteboard video: a plan of scenes for the whole
narration, then each scene's description (docs/video-overview-spec.md,
sections 4.3 and 4.4).

A plan that does not cover the narration in order is replaced by one scene
per part. A scene that does not validate, or that the lint finds fault
with, is sent back once with exactly what is wrong; one still wrong after
that is drawn as a plain scene from its part's own copy. A video is never
lost to one scene.
"""

import asyncio
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import ValidationError

from opennotebook.ai import client
from opennotebook.ai.errors import AiError
from opennotebook.build import timeline as tl
from opennotebook.build.whiteboard import check, ground, icons, lint
from opennotebook.build.whiteboard.compile import compile_scene
from opennotebook.build.whiteboard.scene import COLS, Beat, Element, Plan, PlannedScene, Scene
from opennotebook.domain.sessions import Part

log = logging.getLogger(__name__)

MAX_TOKENS = 6_000
# A scene's first answer and up to two repairs (spec 4.7: two rounds pay off,
# more rarely do).
ATTEMPTS = 3
# Scene calls at once.
PARALLEL = 6
# Markers the stand-in model in the tests answers by.
PLAN_MARK = "You plan a whiteboard explainer video."
SCENE_MARK = "You draw one scene of a whiteboard explainer video."

PLAN_RULES = f"""{PLAN_MARK}
You get the narration, line by line, with each line's id, its part and how long it is spoken.
Group the lines into scenes. Each scene is one idea a teacher draws on a whiteboard while the
narration explains it.

Rules:
- Every line id appears in exactly one scene, in the narration's order, and a scene's lines
  are consecutive.
- A scene lasts 10 to 25 seconds where the narration allows; never split a sentence.
- "concepts": 2 to 6 concrete things to draw, as short nouns ("kernel", "hard disk", "penguin").
  Only what the lines say. Nothing decorative.
- "layout": one of stack, flow, hub, compare, equation, timeline, cycle, illustration, free.
- "title": at most 4 words.
- "brief": one sentence on what the board shows.

Answer with JSON only, no prose and no code fence:
{{"scenes": [{{"lines": ["<id>", ...], "layout": "...", "title": "...", "brief": "...",
"concepts": ["...", ...]}}]}}"""

SCENE_RULES = f"""{SCENE_MARK}
The board is a grid of 6 columns ({COLS[0]} to {COLS[-1]}, left to right) and 6 rows (1 to 6,
top to bottom). You place elements in cells; the studio draws them, so you never give
coordinates.

Elements:
- "icon": an icon from the candidates given, by its exact name, with a short label.
- "box", "circle": a shape with a label inside.
- "label": words alone. "number": a figure ("1991", "75%") with an optional label.
- "arrow" / "line": joins two elements by their ids ("from", "to"), with an optional label.
- "sketch": only when no candidate icon fits. "d" is a simple SVG path in a 100x100 box.

Each element: "id", "kind", "at" (its top-left cell, e.g. "B2"), "span" ([columns, rows],
default [1, 1]), "label", "tone" (ink, blue, red, amber or green), and "beat":
{{"line": "<line id>", "word": <index>}}, the word of the narration it is drawn on. Draw a thing
on the word that names it.

Rules:
- 3 to 12 elements. Spread them over the grid; use most of it. No two elements share a cell.
- An icon with a label needs 2 rows; a box with a label at least 2 columns.
- Labels are 1 to 3 words, taken from the narration. Never write a sentence on the board.
- A number appears only if the narration says it.
- Nothing decorative: draw only what the lines mention.
- An icon must show the thing itself. A candidate that only shares a word with it (a folder
  for "roots", a logout arrow for "leaves") misleads: use a "box" or "label" instead.
- At most 2 "highlight" entries: {{"target": "<id>", "beat": {{...}}}}, to point at the
  element being discussed.
- Every word written on the board (title, labels, figures) is taken from this scene's narration
  or the source passages. Nothing else is written.
- "claims": what the scene asserts, as short sentences, each with the ids of the passages that
  support it: {{"text": "...", "passages": ["p1"]}}. An arrow asserts that its ends are related;
  draw only relations the narration or the passages state.

Answer with JSON only, no prose and no code fence:
{{"title": "...", "layout": "...", "elements": [...], "highlight": [...], "claims": [...]}}"""


@dataclass
class Built:
    """One scene as it will be drawn, and how it got there."""

    planned: PlannedScene
    scene: Scene
    start_ms: float
    end_ms: float
    first_try: bool
    repaired: bool = False
    plain: bool = False
    # Written again by the stronger model after the repairs failed.
    escalated: bool = False
    problems: list[str] = field(default_factory=list[str])
    # The accuracy checks of the scene as drawn: its pieces of text and how
    # many are grounded; its claims and how many the check found supported;
    # whether the check could be made.
    texts: int = 0
    grounded: int = 0
    claims: int = 0
    supported: int = 0
    checked: bool = False


def _json(text: str) -> Any:
    """The first whole JSON object in a reply, fenced or not, whatever
    follows it: a model sometimes adds a note, or a second object."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    return value


def _numbered(text: str) -> str:
    return " ".join(f"[{i}]{w}" for i, w in enumerate(tl.tokens_of(text)))


async def _complete(model: str, system: str, user: str) -> str:
    done = await client.ai().complete(
        model,
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        max_tokens=MAX_TOKENS,
    )
    return done.text


# ── the plan ─────────────────────────────────────────────────────────────────


def default_plan(parts: list[Part]) -> Plan:
    """One scene per part: what a plan that cannot be used is replaced by."""
    scenes = [
        PlannedScene(
            lines=[ln.line_id for ln in p.lines_in_order()],
            layout="free",
            title=p.title[:40],
            brief=p.title,
            concepts=_concepts_of(p),
        )
        for p in parts
        if p.lines
    ]
    return Plan(scenes=scenes)


def _concepts_of(p: Part) -> list[str]:
    out: list[str] = []
    for e in p.on_slide:
        _, _, body = e.partition(":")
        words = (body or e).strip()
        if words:
            out.append(" ".join(words.split()[:4]))
    return out[:5] or [p.title]


def plan_problems(plan: Plan, order: list[str]) -> list[str]:
    got = [lid for sc in plan.scenes for lid in sc.lines]
    if got != order:
        return ["the scenes do not cover every line exactly once, in order"]
    return []


async def plan(model: str, parts: list[Part], timing: tl.Timeline) -> tuple[Plan, bool]:
    """The scenes, and whether the model's plan was used."""
    spans = {sp.line_id: sp for sp in timing.lines}
    order = [sp.line_id for sp in timing.lines]
    rows: list[str] = []
    for p in parts:
        rows.append(f"## Part {p.ordinal}: {p.title}")
        for ln in p.lines_in_order():
            sp = spans.get(ln.line_id)
            secs = (sp.end_ms - sp.start_ms) / 1000 if sp else 0
            rows.append(f"{ln.line_id} ({secs:.1f} s): {ln.text}")
    try:
        got = Plan.model_validate(_json(await _complete(model, PLAN_RULES, "\n".join(rows))))
        if not plan_problems(got, order):
            return got, True
        log.info("the scene plan does not cover the narration; one scene per part instead")
    except (ValueError, ValidationError) as e:
        log.info("the scene plan did not read: %s", e)
    return default_plan(parts), False


# ── a scene ──────────────────────────────────────────────────────────────────


def scene_problems(sc: Scene, lines: dict[str, int]) -> list[str]:
    """What is wrong with a scene before it is drawn: beats on lines the
    scene does not have or past their last word, ids repeated, joins to ids
    that are not there."""
    found: list[str] = []
    ids = [e.id for e in sc.elements if e.id]
    if len(ids) != len(set(ids)):
        found.append("two elements share an id")
    beats: list[tuple[str, Beat]] = [(e.id or e.kind, e.beat) for e in sc.elements]
    beats += [(f"highlight of {h.target}", h.beat) for h in sc.highlight]
    for what, b in beats:
        if b.line not in lines:
            found.append(f"{what} is drawn on line {b.line!r}, which is not one of this scene's")
        elif b.word >= lines[b.line]:
            found.append(f"{what} is drawn on word {b.word} of {b.line}, which has {lines[b.line]}")
    known = set(ids)
    for e in sc.elements:
        if e.kind in ("arrow", "line") and not {e.source, e.target} <= known:
            found.append(f"{e.kind} {e.id or '?'} joins ids that are not in the scene")
        elif e.kind in ("arrow", "line") and e.source == e.target:
            found.append(f"{e.kind} {e.id or '?'} joins {e.source!r} to itself")
        if e.kind not in ("arrow", "line") and e.at is None:
            found.append(f"{e.kind} {e.id or '?'} has no cell")
        if e.kind in ("box", "circle", "label") and not e.label.strip():
            # An empty shape says nothing, and the viewer is left to guess.
            found.append(f"{e.kind} {e.id or '?'} has no label: name what it stands for")
        if e.kind == "icon" and e.icon and not icons.exists(e.icon):
            found.append(f"icon {e.icon!r} is not in the library; use a candidate's exact name")
    return found


def _word_of(concept: str, lines: dict[str, list[str]]) -> tuple[str, int] | None:
    """Where the narration first names a concept: the line and the index of
    the word that starts it."""
    want = [w for w in re.findall(r"[a-z0-9]+", concept.lower()) if w not in icons.STOP]
    for lid, words in lines.items():
        for i, w in enumerate(words):
            core = "".join(c for c in w.lower() if c.isalnum())
            if want and (core == want[0] or core.rstrip("s") == want[0].rstrip("s")):
                return lid, i
    return None


def plain_scene(
    ps: PlannedScene,
    lines: dict[str, list[str]],
    cands: dict[str, list[str]],
    said: ground.Said | None = None,
) -> Scene:
    """A scene the studio lays out itself, for when the model's would not do:
    the concepts left to right, each drawn on the word that names it (or on
    its share of the lines when none does), joined by arrows when there are
    few enough to leave them room."""
    ids = list(lines)
    concepts = [c for c in ps.concepts if c.strip()]
    title = ps.title
    if said is not None:
        # The plain scene writes nothing that was not said either: the plan's
        # concepts that the narration or sources name, else the narration's
        # own longest words.
        concepts = [c for c in concepts if not said.unsaid(c)]
        if not concepts:
            flat = [w.strip(".,;:!?\"'()") for ws in lines.values() for w in ws]
            longest = sorted({w for w in flat if w.lower() not in ground.FILLER}, key=len)
            concepts = list(reversed(longest))[:3]
        if said.unsaid(title):
            title = ""
    concepts = concepts[:4] or [ps.title or "idea"]
    n = len(concepts)
    span = max(1, 6 // n)
    first = (6 - span * n) // 2
    elements: list[Element] = []
    for i, c in enumerate(concepts):
        at = _word_of(c, lines) or (ids[min(i * len(ids) // n, len(ids) - 1)], 0)
        icon = next(iter(cands.get(c) or icons.search(c, 1)), None)
        elements.append(
            Element.model_validate(
                {
                    "id": f"c{i}",
                    "kind": "icon" if icon else "box",
                    "icon": icon,
                    "at": f"{COLS[first + i * span]}2",
                    "span": [span, 3],
                    "label": " ".join(c.split()[:3]),
                    "beat": {"line": at[0], "word": at[1]},
                }
            )
        )
    if span >= 2:
        for i in range(n - 1):
            b = elements[i + 1].beat
            elements.append(
                Element.model_validate(
                    {"id": f"j{i}", "kind": "arrow", "from": f"c{i}", "to": f"c{i + 1}",
                     "beat": {"line": b.line, "word": b.word}}
                )
            )  # fmt: skip
    return Scene(title=title, layout="flow", elements=elements)


def _scene_prompt(
    ps: PlannedScene,
    parts: list[Part],
    cands: dict[str, list[str]],
    passages: list[str] | None = None,
) -> str:
    rows = [f"Title: {ps.title}", f"Layout: {ps.layout}", f"What the board shows: {ps.brief}", ""]
    rows.append("The narration of this scene, each word numbered:")
    by_id = {ln.line_id: ln for p in parts for ln in p.lines}
    for lid in ps.lines:
        if lid in by_id:
            rows.append(f"{lid}: {_numbered(by_id[lid].text)}")
    rows.append("")
    rows.append("Candidate icons for each concept (use these exact names):")
    for c, names in cands.items():
        # With each icon's category: a name can mean something else ("mars"
        # is the male sign, filed under Gender).
        shown = [f"{n} ({icons.category(n)})" for n in names]
        rows.append(f"- {c}: {', '.join(shown) if shown else '(none: use a box or a sketch)'}")
    if passages:
        rows += ["", 'Source passages the video is made from (cite them by id in "claims"):']
        rows += [f"p{i + 1}: {p}" for i, p in enumerate(passages)]
    return "\n".join(rows)


async def draw_scene(
    model: str,
    ps: PlannedScene,
    parts: list[Part],
    when: Callable[[Beat], float],
    start_ms: float,
    end_ms: float,
    *,
    passages: list[str] | None = None,
    check_model: str = "",
    escalate_model: str = "",
) -> Built:
    """One scene: written, then checked (its form, its words against what
    was said, its geometry, then its claims and its picture), and repaired
    with exactly what is wrong; then once by `escalate_model`; drawn plain
    as a last resort, from its own grounded words."""
    by_id = {ln.line_id: ln for p in parts for ln in p.lines}
    words = {lid: tl.tokens_of(by_id[lid].text) for lid in ps.lines if lid in by_id}
    lines = {lid: len(w) for lid, w in words.items()}
    narration = [by_id[lid].text for lid in ps.lines if lid in by_id]
    sources = list(passages or [])
    said = ground.Said.of(narration + sources)
    cands = icons.candidates(ps.concepts or [ps.title])
    prompt = _scene_prompt(ps, parts, cands, sources)
    messages = prompt
    problems: list[str] = []
    writers = [model] * ATTEMPTS
    if escalate_model and escalate_model != model:
        writers.append(escalate_model)
    for attempt, writer in enumerate(writers):
        sc: Scene | None = None
        verdict = check.Verdict(checked=False)
        try:
            sc = Scene.model_validate(_json(await _complete(writer, SCENE_RULES, messages)))
            problems = scene_problems(sc, lines) + ground.problems(sc, said)
            if not problems:
                problems = lint.problems(compile_scene(sc, when, start_ms, end_ms))
            if not problems and check_model:
                png = await asyncio.to_thread(check.still, sc, when, start_ms, end_ms)
                verdict = await check.check(check_model, sc, png, narration, sources)
                problems = verdict.problems
                # The picture's advice is taken once: the first answer is
                # sent back with it; a later one stands on its claims.
                if not problems and attempt == 0:
                    problems = verdict.advice
        except AiError:
            if attempt < ATTEMPTS:
                raise
            # The stronger model is out of reach: the plain scene stands in.
            break
        except (ValueError, ValidationError) as e:
            sc, problems = None, [f"the answer is not a valid scene: {str(e)[:400]}"]
        if not problems and sc is not None:
            texts, grounded = ground.counts(sc, said)
            return Built(
                ps, sc, start_ms, end_ms,
                first_try=attempt == 0, repaired=0 < attempt < ATTEMPTS,
                escalated=attempt >= ATTEMPTS,
                texts=texts, grounded=grounded,
                claims=verdict.claims, supported=verdict.supported, checked=verdict.checked,
            )  # fmt: skip
        log.info("scene %s needs another try: %s", ps.title, "; ".join(problems))
        messages = (
            f"{prompt}\n\nYour last answer had these problems; fix every one and answer "
            "with the whole scene again:\n- " + "\n- ".join(problems)
        )
    plain = plain_scene(ps, words, cands, said)
    texts, grounded = ground.counts(plain, said)
    return Built(
        ps, plain, start_ms, end_ms, first_try=False, plain=True, problems=problems,
        texts=texts, grounded=grounded,
    )  # fmt: skip


def windows(plan_: Plan, timing: tl.Timeline) -> list[tuple[float, float]]:
    """When each scene is on screen: from where the last one gives way until
    the next one's first line starts. A scene holds through the pause after
    its last line, so its last drawing gets that pause to finish and be seen
    before the board is wiped."""
    spans = {sp.line_id: sp for sp in timing.lines}
    out: list[tuple[float, float]] = []
    scenes = plan_.scenes
    for i in range(len(scenes)):
        start = out[-1][1] if out else 0.0
        end = spans[scenes[i + 1].lines[0]].start_ms if i + 1 < len(scenes) else timing.total_ms
        out.append((start, float(end)))
    return out


async def draw_all(
    model: str,
    plan_: Plan,
    parts: list[Part],
    timing: tl.Timeline,
    when: Callable[[Beat], float],
) -> list[Built]:
    """Every scene, `PARALLEL` at a time."""
    gate = asyncio.Semaphore(PARALLEL)

    async def one(ps: PlannedScene, w: tuple[float, float]) -> Built:
        async with gate:
            return await draw_scene(model, ps, parts, when, *w)

    spans = windows(plan_, timing)
    return list(
        await asyncio.gather(*(one(ps, w) for ps, w in zip(plan_.scenes, spans, strict=True)))
    )
