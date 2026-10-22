"""What the model writes for a motion video: one page for the whole
narration, and the manifest the page is checked with
(`docs/motion-style.md`).

The manifest is the page's declared intent: what it puts on screen, when,
and what it asserts. It is what the studio holds the page to — every word
written on screen must come from the narration or the sources (the
deterministic check the whiteboard uses, over the manifest instead of over
a scene's elements), and everything declared must be placed where the lint
can measure it (`lint.py`, through `data-mn` in the recorded page). The
manifest survives into the video's script, so the watch page's explainer
can read what the viewer has just seen.

Each manifest entry is one scene of the plan, with its span merged in: the
page reads `window.__SCENES` to know what belongs where, and the lint and
the check work scene by scene.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from opennotebook.build.whiteboard.scene import Beat

Kind = Literal["text", "card", "figure", "bar", "icon", "line", "arrow", "panel", "mark"]


class Node(BaseModel):
    """One thing the page puts on screen, at its beat."""

    model_config = ConfigDict(extra="ignore")

    id: str
    kind: Kind
    # What is written on screen: a label of one to three words, or a figure.
    # Grounded against the narration and the sources like a board's labels.
    text: str = ""
    # What it stands for, for the vision check and the explainer. Not
    # written anywhere, so it is not held to the narration's words.
    about: str = ""
    # The word it appears on: a narration line and the word's index in it,
    # counted from 0, exactly as a whiteboard element's `beat` is.
    beat: Beat
    # What it joins (`arrow`, `line`), by the ids of its two ends.
    source: str | None = Field(default=None, alias="from")
    target: str | None = Field(default=None, alias="to")
    # Which scene of the plan it belongs to: the scene's index (0 for the
    # first), or a narration line id, which resolves to the scene covering
    # that line. Null joins it to the scene of its own beat.
    scene: int | str | None = None
    # For a `figure` or `bar`: the value it shows, as a number or percent.
    value: float | None = None
    # For an `icon`: the thing's name, which the page draws as a simple
    # glyph of its own (a motion page bundles no icon library).
    glyph: str = ""

    @field_validator("text", "about")
    @classmethod
    def _short(cls, v: str) -> str:
        return " ".join(v.split())[:280]

    @property
    def shows(self) -> str:
        """What the checker and the explainer call this thing."""
        return self.text or self.about or self.glyph or self.id


class Claim(BaseModel):
    """What a scene asserts, with the passages that support it: a whiteboard
    scene's claim, its scene index added."""

    model_config = ConfigDict(extra="ignore")

    text: str
    passages: list[str] = Field(default_factory=list[str])
    scene: int | str | None = None


class Page(BaseModel):
    """The whole motion page as the model writes it: its HTML, and what it
    declares it puts on screen."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    html: str = Field(min_length=1)
    elements: list[Node] = Field(default_factory=list[Node], max_length=120)
    claims: list[Claim] = Field(default_factory=list[Claim])

    @field_validator("html")
    @classmethod
    def _whole(cls, v: str) -> str:
        low = v.lower()
        for bad in ("http://", "https://", "<script src", "<link", "fetch(", "xmlhttprequest"):
            if bad in low:
                raise ValueError(
                    f"the page must reach nothing outside itself ({bad!r} is in it): the "
                    "recording has no network, so anything it fetches never loads"
                )
        # A doctype that is not the first thing is ignored, and the page
        # would record in quirks mode; a missing </head> leaves the studio
        # nowhere to splice its script in but before the doctype.
        if not v.lstrip().lower().startswith("<!doctype"):
            raise ValueError("the page must start with <!doctype html>")
        if "</head>" not in low:
            raise ValueError("the page needs a <head> closed with </head>, for the studio's script")
        if "</script>" not in low:
            raise ValueError("the page needs its own <script>, closed with </script>, to draw with")
        return v


def resolve(page: Page, scenes_of_line: dict[str, int]) -> tuple[list[Node], list[Claim]]:
    """The manifest with every `scene` as a plan scene's index: a line id
    becomes the scene covering that line, null becomes the scene of the
    item's own beat, and a line no scene covers (so no beat may name it
    either) falls back to the first scene."""
    first = 0

    def scene_of(value: int | str | None, beat_line: str) -> int:
        if isinstance(value, int):
            return max(value, first)
        return scenes_of_line.get(str(value or beat_line), first)

    nodes = [n.model_copy(update={"scene": scene_of(n.scene, n.beat.line)}) for n in page.elements]
    claims = [
        c.model_copy(
            update={
                "scene": scene_of(c.scene, (page.elements[0].beat.line if page.elements else ""))
            }
        )
        for c in page.claims
    ]
    return nodes, claims


def manifest(
    titles: list[str], windows: list[tuple[float, float]], nodes: list[Node], claims: list[Claim]
) -> list[dict[str, Any]]:
    """The manifest as `page.inject` splices it into the page: one entry a
    scene of the plan, with its span and what appears in it."""
    out: list[dict[str, Any]] = []
    for i, (start, end) in enumerate(windows):
        mine, said = [n for n in nodes if n.scene == i], [c for c in claims if c.scene == i]
        out.append(
            {
                "i": i,
                "title": titles[i] if i < len(titles) else "",
                "start_ms": round(start),
                "end_ms": round(end),
                "elements": [n.model_dump(mode="json", by_alias=True) for n in mine],
                "claims": [c.model_dump(mode="json") for c in said],
            }
        )
    return out


def labels_of(scene: dict[str, Any]) -> list[str]:
    """One scene's words on screen, as the video's script keeps them. The
    scene's title is a heading and is not a label, as a board's is not."""
    return [n["text"] for n in scene.get("elements") or [] if str(n.get("text", "")).strip()]


def claims_of(scene: dict[str, Any]) -> list[str]:
    return [str(c.get("text", "")) for c in scene.get("claims") or [] if str(c.get("text", ""))]


def nodes_of(manifest_: list[dict[str, Any]], i: int) -> list[Node]:
    """One scene's declared elements, back as nodes: what the lint and the
    check are given."""
    return [Node.model_validate(n) for n in manifest_[i].get("elements") or []]


def claims_in(manifest_: list[dict[str, Any]], i: int) -> list[Claim]:
    return [Claim.model_validate(c) for c in manifest_[i].get("claims") or []]
