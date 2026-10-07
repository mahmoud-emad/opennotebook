"""What the model writes for a whiteboard video: a plan of scenes, and each
scene as a short description the compiler draws (docs/video-overview-spec.md,
section 4.4 and Appendix A).

The model never places a pixel. It names cells of a 6×6 grid, icons from
the bundled library, short labels, arrows between elements, and the word of
the narration each thing is drawn on. Everything else (coordinates, strokes,
colours, fonts, timing) is the compiler's, so a scene the model writes
cannot overlap by a pixel it chose.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

COLS = "ABCDEF"
ROWS = 6
CELL = re.compile(r"^([A-F])([1-6])$")

Tone = Literal["ink", "blue", "red", "amber", "green"]
Kind = Literal["icon", "box", "circle", "label", "number", "arrow", "line", "sketch"]
Layout = Literal[
    "stack", "flow", "hub", "compare", "equation", "timeline", "cycle", "illustration", "free"
]

LAYOUTS: tuple[str, ...] = Layout.__args__  # pyright: ignore[reportAttributeAccessIssue]

# Labels are names, not sentences: the narration says the sentence.
LABEL_CHARS = 28


class Beat(BaseModel):
    """The word an element is drawn on: a line of the narration and the
    word's index in that line's text, counting from 0."""

    model_config = ConfigDict(extra="ignore")

    line: str
    word: int = Field(default=0, ge=0)


class Element(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    id: str = ""
    kind: Kind
    # A cell from A1 (top left) to F6, and how many columns and rows it
    # takes from there. Arrows and lines have none: they join two elements.
    at: str | None = None
    span: tuple[int, int] = (1, 1)
    icon: str | None = None
    label: str = ""
    # A number's figure, as it is said: "1991", "16", "75%".
    text: str = ""
    tone: Tone = "ink"
    beat: Beat
    source: str | None = Field(default=None, alias="from")
    target: str | None = Field(default=None, alias="to")
    # A sketch's own path, in a 100×100 box.
    d: str | None = None

    @field_validator("tone", mode="before")
    @classmethod
    def _tone(cls, v: object) -> object:
        # A colour the board does not have is drawn in ink, not refused.
        return v if v in ("ink", "blue", "red", "amber", "green") else "ink"

    @field_validator("at")
    @classmethod
    def _cell(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().upper()
        if not CELL.match(v):
            raise ValueError(f"{v!r} is not a cell from A1 to F6")
        return v

    @field_validator("span")
    @classmethod
    def _span(cls, v: tuple[int, int]) -> tuple[int, int]:
        cols, rows = v
        if not (1 <= cols <= len(COLS) and 1 <= rows <= ROWS):
            raise ValueError(f"span {v} is outside the grid")
        return v

    @field_validator("label", "text")
    @classmethod
    def _short(cls, v: str) -> str:
        return " ".join(v.split())[:LABEL_CHARS]


class Highlight(BaseModel):
    model_config = ConfigDict(extra="ignore")

    target: str
    beat: Beat


class Claim(BaseModel):
    """What a scene asserts, with the passages that support it; checked in
    phase 3."""

    model_config = ConfigDict(extra="ignore")

    text: str
    passages: list[str] = Field(default_factory=list[str])


def _layout(v: object) -> object:
    """A layout the list does not name (a model may describe one in words)
    is "free": a hint, not worth refusing a scene over."""
    return v if v in LAYOUTS else "free"


class Scene(BaseModel):
    model_config = ConfigDict(extra="ignore")

    title: str = ""
    layout: Layout = "free"

    elements: list[Element] = Field(min_length=1, max_length=40)
    highlight: list[Highlight] = Field(default_factory=list[Highlight])
    claims: list[Claim] = Field(default_factory=list[Claim])

    @field_validator("layout", mode="before")
    @classmethod
    def _layout(cls, v: object) -> object:
        return _layout(v)


class PlannedScene(BaseModel):
    """One scene of the plan: the narration lines it covers, in order, and
    what to draw for them."""

    model_config = ConfigDict(extra="ignore")

    lines: list[str] = Field(min_length=1)
    layout: Layout = "free"

    title: str = ""
    brief: str = ""
    concepts: list[str] = Field(default_factory=list[str])

    @field_validator("layout", mode="before")
    @classmethod
    def _layout(cls, v: object) -> object:
        return _layout(v)


class Plan(BaseModel):
    model_config = ConfigDict(extra="ignore")

    scenes: list[PlannedScene] = Field(min_length=1)


def cell(at: str) -> tuple[int, int]:
    """A cell's column and row, from 0."""
    m = CELL.match(at)
    assert m is not None
    return COLS.index(m.group(1)), int(m.group(2)) - 1
