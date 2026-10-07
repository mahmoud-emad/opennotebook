"""A scene's picture, for an illustrated theme (docs/plans/video-themes.md,
phase 4).

An illustrated video is a whiteboard video whose scenes are shown as a
picture rather than drawn: the plan, the scene, its grounding and its claims
check are a drawn scene's, and only the board is painted instead. So what the
image model is told is the scene's brief and its concepts, never its labels,
and it is told to write nothing: words in a picture are the one thing a
picture gets wrong that a viewer believes. The labels are the studio's own,
set over the picture (`compile.compile_illustrated`).

Each picture is checked before it is used, by the model that checks a drawn
scene, for two faults: lettering of any kind, and anything the scene's brief
contradicts. A picture with a fault is made once more with the fault named;
one that still fails, or that cannot be made, leaves the scene to be drawn in
the theme's drawn twin. A video never fails for a picture.
"""

import base64
import json
import logging
from dataclasses import dataclass

import skia

from opennotebook.ai import client
from opennotebook.ai.errors import AiError
from opennotebook.build.whiteboard.scene import PlannedScene
from opennotebook.build.whiteboard.theme import Theme

log = logging.getLogger(__name__)

ASPECT = "16:9"
# A custom style, in the person's own words, at most this long.
CUSTOM_CHARS = 200
ATTEMPTS = 2

RULES = """Paint one picture for a scene of an educational explainer video.
Style: {style}
What the scene is about: {brief}
Show: {concepts}.

Rules:
- No text, letters, numbers, labels, captions, signs or symbols of any kind, anywhere.
- Show only what is described above. Nothing decorative that could be mistaken for a fact.
- Keep the bottom third of the picture calm and low in detail: words will be set there.
- One clear composition, wide, 16:9."""

CHECK = """You check a picture made for an educational video before it is shown.
The scene it is for: {brief}
Answer with JSON only: {{"lettering": true|false, "contradicts": true|false, "why": "..."}}
- "lettering": the picture contains any text, letters, numbers or writing-like marks.
- "contradicts": the picture shows something the scene's description says is not so, or that
  would mislead a viewer about it."""


@dataclass
class Picture:
    """A scene's picture, PNG at the video's size, or why there is none."""

    png: bytes | None
    attempts: int
    faults: list[str]


def prompt(theme: Theme, ps: PlannedScene, custom: str = "") -> str:
    """What the image model is asked for: the theme's style (or the person's
    own, for a custom theme), the scene's brief and concepts. Never the
    scene's labels: a picture is not to carry words."""
    style = " ".join(custom.split())[:CUSTOM_CHARS] if custom.strip() else theme.style
    concepts = ", ".join(c for c in ps.concepts if c.strip()) or ps.title or "the idea"
    return RULES.format(style=style, brief=ps.brief or ps.title, concepts=concepts)


def _fit(data: bytes) -> bytes | None:
    """The picture as a PNG at 1920×1080, cropped to fill; None for one that
    is not a picture."""
    img = skia.Image.MakeFromEncoded(skia.Data.MakeWithCopy(data))
    if img is None or img.width() < 64 or img.height() < 64:
        return None
    w, h = img.width(), img.height()
    scale = max(1920 / w, 1080 / h)
    sw, sh = 1920 / scale, 1080 / scale
    src = skia.Rect.MakeXYWH((w - sw) / 2, (h - sh) / 2, sw, sh)
    surface = skia.Surface(1920, 1080)
    surface.getCanvas().drawImageRect(
        img, src, skia.Rect.MakeWH(1920, 1080), skia.SamplingOptions(skia.FilterMode.kLinear)
    )
    out = surface.makeImageSnapshot().encodeToData()
    return bytes(out) if out is not None else None


async def _check(model: str, png: bytes, ps: PlannedScene) -> list[str]:
    """What is wrong with a picture: lettering, or a contradiction of its
    brief. A checker that cannot answer finds nothing wrong: the picture
    carries no claims of its own, the labels over it were checked."""
    url = "data:image/png;base64," + base64.b64encode(png).decode()
    content = [
        {"type": "text", "text": CHECK.format(brief=ps.brief or ps.title)},
        {"type": "image_url", "image_url": {"url": url}},
    ]
    try:
        done = await client.ai().complete(
            model, [{"role": "user", "content": content}], max_tokens=300
        )
        start = done.text.find("{")
        v = json.loads(done.text[start:]) if start >= 0 else {}
    except (AiError, ValueError) as e:
        log.info("the picture check did not answer: %s", e)
        return []
    faults: list[str] = []
    if v.get("lettering") is True:
        faults.append("the picture has lettering in it; it must have none at all")
    if v.get("contradicts") is True:
        faults.append(f"the picture misleads about the scene: {str(v.get('why', ''))[:200]}")
    return faults


async def picture(
    model: str, check_model: str, theme: Theme, ps: PlannedScene, custom: str = ""
) -> Picture:
    """The scene's picture, checked; or none, with why."""
    ask = prompt(theme, ps, custom)
    faults: list[str] = []
    for attempt in range(1, ATTEMPTS + 1):
        try:
            done = await client.ai().complete(
                model, [{"role": "user", "content": ask}], image_aspect=ASPECT
            )
        except AiError as e:
            log.info("the picture for %r could not be made: %s", ps.title, e)
            return Picture(None, attempt, [f"the image model failed: {e}"])
        png = next((p for p in (_fit(d) for d in done.images) if p is not None), None)
        if png is None:
            faults = ["the image model sent no picture"]
            continue
        faults = await _check(check_model, png, ps) if check_model else []
        if not faults:
            return Picture(png, attempt, [])
        log.info("the picture for %r needs another try: %s", ps.title, "; ".join(faults))
        ask = (
            f"{prompt(theme, ps, custom)}\n\nThe last picture had these faults; avoid them:\n- "
            + ("\n- ".join(faults))
        )
    return Picture(None, ATTEMPTS, faults)
