"""A scene judged before it is drawn into the video: what it claims, against
its narration and source passages, and what its finished board shows,
looked at (docs/video-overview-spec.md, section 4.7).

One call per scene, to a model that reads images: the finished board drawn
small, the scene's claims and the relations its arrows draw written out as
sentences, the narration and the passages. It answers yes or no, never a
score: vision models judge pass or fail reliably and scores poorly
(section 2.2). What it finds goes back to the writer as a repair, word for
word.

A check that cannot be made (the model is down, or answers something else)
does not stop the video: the scene is kept, and counted as unchecked.
"""

import base64
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import skia

from opennotebook.ai import client
from opennotebook.ai.errors import AiError
from opennotebook.build.whiteboard import theme as th
from opennotebook.build.whiteboard.compile import H, W, compile_scene
from opennotebook.build.whiteboard.draw import draw_piece, paper
from opennotebook.build.whiteboard.scene import Beat, Scene

# skia-python ships without complete type information.
# pyright: reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

log = logging.getLogger(__name__)

CHECK_MARK = "You check one scene of a whiteboard explainer video before it is shown."
# The board as the checker sees it: big enough to read every label.
SEEN = (960, 540)

RULES = f"""{CHECK_MARK}
You get the narration spoken over the scene, the source passages the video is made from, the
claims the scene makes (including what its arrows say), the things it draws, and a picture of
the finished board.

Judge:
1. Each claim, against the narration and the passages only, never the picture: "supported" is
   true only if they say it. A claim that adds a fact, overstates, or reverses a relation they
   state is not supported. A claim that says less than they do is supported.
2. The picture, only for the things the scene draws (listed):
   - "missing": the listed things a viewer could not recognise in the picture.
   - "unreadable": labels that are cut off, covered, overlapping or too small to read.
   - "matches_narration": false only if the board shows something the narration contradicts.
     A board that shows less than the narration says still matches.
   - "fixes": for every problem, one short instruction to the person drawing it.

Answer with JSON only, no prose and no code fence:
{{"claims": [{{"claim": "...", "supported": true, "why": "..."}}],
"missing": [], "unreadable": [], "matches_narration": true, "fixes": []}}"""


@dataclass
class Verdict:
    # What the scene claims that the sources do not say: a fault the scene
    # is never drawn with.
    problems: list[str] = field(default_factory=list[str])
    # What the picture looks like it gets wrong: advice. Vision models judge
    # pictures less reliably than text (spec 2.2); one real run had scenes
    # refused for a misread arrowhead and for not drawing every noun the
    # narration said. So it earns one repair, not a refusal.
    advice: list[str] = field(default_factory=list[str])
    claims: int = 0
    supported: int = 0
    checked: bool = True


def still(
    sc: Scene,
    when: Callable[[Beat], float],
    start_ms: float,
    end_ms: float,
    theme: th.Theme | str | None = None,
) -> bytes:
    """The scene's finished board as a PNG, at the size the checker reads,
    in the theme it will be drawn in (or the one in use)."""
    with th.using(theme if theme is not None else th.current()):
        d = compile_scene(sc, when, start_ms, end_ms)
        surface = skia.Surface(SEEN[0], SEEN[1])
        c = surface.getCanvas()
        c.scale(SEEN[0] / W, SEEN[1] / H)
        paper(c)
        for p in sorted(d.pieces, key=lambda p: p.start_ms):
            draw_piece(c, p, 1.0)
    data = surface.makeImageSnapshot().encodeToData()
    return bytes(data) if data is not None else b""


def relations(sc: Scene) -> list[str]:
    """What the scene asserts: its claims, and each arrow as a sentence."""
    names = {e.id: (e.label or e.text or e.icon or e.id) for e in sc.elements if e.id}
    out = [c.text for c in sc.claims if c.text.strip()]
    for e in sc.elements:
        if e.kind in ("arrow", "line") and e.source in names and e.target in names:
            verb = f" {e.label} " if e.label else (" leads to " if e.kind == "arrow" else " — ")
            out.append(f"{names[e.source]}{verb}{names[e.target]}")
    return out


def _json(text: str) -> Any:
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    value, _ = json.JSONDecoder().raw_decode(text[start:])
    return value


async def check(
    model: str, sc: Scene, png: bytes, narration: list[str], passages: list[str]
) -> Verdict:
    claims = relations(sc)
    rows = ["Narration:", *narration, "", "Source passages:"]
    rows += [f"[{i + 1}] {p}" for i, p in enumerate(passages)] or ["(none)"]
    rows += ["", "Claims the scene makes:"]
    rows += [f"- {c}" for c in claims] or ["(none)"]
    drawn = [
        e.label or e.text or e.icon or "" for e in sc.elements if e.kind not in ("arrow", "line")
    ]
    rows += ["", "Things the scene draws:"]
    rows += [f"* {d}" for d in drawn if d] or ["(none)"]
    image = "data:image/png;base64," + base64.b64encode(png).decode()
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": RULES},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "\n".join(rows)},
                {"type": "image_url", "image_url": {"url": image}},
            ],
        },
    ]
    try:
        done = await client.ai().complete(model, messages, max_tokens=1500)
        v = _json(done.text)
    except (AiError, ValueError) as e:
        log.info("the scene check could not be made: %s", e)
        return Verdict(checked=False)
    if not isinstance(v, dict):
        return Verdict(checked=False)
    return judged(v, len(claims))  # pyright: ignore[reportUnknownArgumentType]


def _strs(v: Any) -> list[str]:
    return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []  # pyright: ignore[reportUnknownVariableType]


def judged(v: dict[str, Any], asked: int) -> Verdict:
    """The checker's answer as problems the writer can act on."""
    out = Verdict()
    for c in v.get("claims") or []:
        if not isinstance(c, dict):
            continue
        out.claims += 1
        if c.get("supported") is True:  # pyright: ignore[reportUnknownMemberType]
            out.supported += 1
        else:
            why = str(c.get("why") or "the sources do not say it")  # pyright: ignore[reportUnknownMemberType]
            out.problems.append(
                f"the claim {str(c.get('claim', ''))!r} is not supported: {why}; "  # pyright: ignore[reportUnknownMemberType]
                "change it to what the narration says, or leave it out"
            )
    # Claims it did not answer for are not counted as supported.
    out.claims = max(out.claims, asked)
    for m in _strs(v.get("missing")):
        out.advice.append(f"{m} is not recognisable on the board")
    for u in _strs(v.get("unreadable")):
        out.advice.append(f"{u} cannot be read")
    if v.get("matches_narration") is False:
        out.advice.append("the board shows something the narration contradicts")
    fixes = [f"fix: {f}" for f in _strs(v.get("fixes"))]
    if out.problems:
        out.problems += fixes
    elif out.advice:
        out.advice += fixes
    return out
