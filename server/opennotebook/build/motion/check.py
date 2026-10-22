"""A motion page's scene judged before it is recorded: what it claims,
against its narration and the source passages, and what its page shows at
the end of the scene, looked at (`docs/video-overview-spec.md` section 4.7).

One call per scene, to a model that reads images: the frame the page paints
at the end of the scene, the scene's claims and what its manifest declares,
the narration and the passages. It answers yes or no, never a score — the
same split the whiteboard's check uses (`whiteboard/check.py`): claims are
faults, judged against the words only; what the picture looks like gets
wrong is advice, earned one repair. A check that cannot be made does not
stop the video: the scene is kept and counted as unchecked.
"""

import base64
import logging
from dataclasses import dataclass, field
from typing import Any

from opennotebook.ai import client
from opennotebook.ai.errors import AiError
from opennotebook.build.motion.reply import json_of
from opennotebook.build.motion.scene import Claim, Node

log = logging.getLogger(__name__)

CHECK_MARK = "You check one scene of an animated explainer video before it is recorded."
# The frame the checker sees: big enough to read its labels.
SEEN = (960, 540)
# The most a verdict may be answered with: a line per claim and a few lists.
MAX_TOKENS = 1_500

RULES = f"""{CHECK_MARK}
You get the narration spoken over the scene, the source passages the video is made from, the
scene's claims, what it puts on screen (id, kind, and what each thing is), and one frame the
page paints at the end of that scene.

Judge:
1. Each claim, against the narration and the passages only, never the frame: "supported" is
   true only if they say it. A claim that adds a fact, overstates, or reverses a relation they
   state is not supported. A claim that says less than they do is supported.
2. The frame, only for what the scene puts on screen (listed):
   - "missing": the listed things a viewer could not recognise in the frame.
   - "unreadable": labels that are cut off, covered, overlapping or too small to read.
   - "matches_narration": false only if the frame shows something the narration contradicts.
     A frame that shows less than the narration says still matches.
   - "fixes": for every problem, one short instruction to the page's writer.

Answer with JSON only, no prose and no code fence:
{{"claims": [{{"claim": "...", "supported": true, "why": "..."}}],
"missing": [], "unreadable": [], "matches_narration": true, "fixes": []}}"""


@dataclass
class Verdict:
    """The checker's judgement of a scene, and what it counted."""

    # What the scene claims that the sources do not say: a fault the page
    # is never recorded with.
    problems: list[str] = field(default_factory=list[str])
    # The texts of those unsupported claims: a scene's claims that fail go
    # out of the manifest, so the video asserts only what was checked.
    unsupported: list[str] = field(default_factory=list[str])
    # What the frame looks like it gets wrong: advice, earned one repair
    # (vision models judge pictures less reliably than text, spec 2.2).
    advice: list[str] = field(default_factory=list[str])
    claims: int = 0
    supported: int = 0
    checked: bool = True


def relations(nodes: list[Node], claims: list[Claim]) -> list[str]:
    """What a scene asserts: its claims, and each arrow or line as a
    sentence, as the whiteboard's check writes them."""
    names = {n.id: (n.text or n.about or n.glyph or n.id) for n in nodes}
    out = [c.text for c in claims if c.text.strip()]
    for n in nodes:
        if n.kind in ("arrow", "line") and n.source in names and n.target in names:
            verb = " → " if n.kind == "arrow" else " — "
            out.append(f"{names[n.source]}{verb}{names[n.target]}")
    return out


async def check(
    model: str, nodes: list[Node], claims: list[Claim], png: bytes, narration: list[str],
    passages: list[str],
) -> Verdict:  # fmt: skip
    """The scene judged by `model`: its claims against the narration and the
    passages, and the frame its page paints (`png`) as a viewer sees it."""
    said = relations(nodes, claims)
    rows = ["Narration:", *narration, "", "Source passages:"]
    rows += [f"[{i + 1}] {p}" for i, p in enumerate(passages)] or ["(none)"]
    rows += ["", "Claims the scene makes:"]
    rows += [f"- {c}" for c in said] or ["(none)"]
    rows += ["", "What it puts on screen:"]
    rows += [f"* {n.kind} {n.id}: {n.text or n.about or n.glyph}" for n in nodes] or ["(none)"]
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
        done = await client.ai().complete(model, messages, max_tokens=MAX_TOKENS)
        v = json_of(done.text)
    except (AiError, ValueError) as e:
        log.info("the scene check could not be made: %s", e)
        return Verdict(checked=False)
    if not isinstance(v, dict):
        return Verdict(checked=False)
    return judged(v, len(said))


def _strs(v: Any) -> list[str]:
    return [str(x) for x in v if str(x).strip()] if isinstance(v, list) else []


def judged(v: dict[str, Any], asked: int) -> Verdict:
    """The checker's answer as problems the writer can act on."""
    out = Verdict()
    for c in v.get("claims") or []:
        if not isinstance(c, dict):
            continue
        out.claims += 1
        if c.get("supported") is True:
            out.supported += 1
        else:
            why = str(c.get("why") or "the sources do not say it")
            out.unsupported.append(str(c.get("claim", "")))
            out.problems.append(
                f"the claim {str(c.get('claim', ''))!r} is not supported: {why}; "
                "change it to what the narration says, or leave it out"
            )
    # Claims it did not answer for are not counted as supported.
    out.claims = max(out.claims, asked)
    for m in _strs(v.get("missing")):
        out.advice.append(f"{m} is not recognisable in the video")
    for u in _strs(v.get("unreadable")):
        out.advice.append(f"{u} cannot be read")
    if v.get("matches_narration") is False:
        out.advice.append("the video shows something the narration contradicts")
    fixes = [f"fix: {f}" for f in _strs(v.get("fixes"))]
    if out.problems:
        out.problems += fixes
    elif out.advice:
        out.advice += fixes
    return out
