"""How a whiteboard video looks: its theme (docs/plans/video-themes.md).

A theme is the whole look in one place: the paper and what is printed on
it, the five inks the scenes name by tone, the highlighter, the pen and its
tip, the hand the labels are written in, and the colours of the opening and
closing slides. The scenes themselves do not change with it: the model
writes `ink | blue | red | amber | green`, and the theme says what each is.

A theme is set where the drawing starts (`compile_scene`, `draw.frames`,
`check.still`, the slides) with `using`, and read below that with
`current()`: the helpers that place a label or wobble a line need it, and
threading it through every one of them would say nothing more. It is plain
data, named by its `id`, so a drawing process is told the id and looks it
up.
"""

import dataclasses
from collections.abc import Generator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Literal

type RGB = tuple[int, int, int]

TONES = ("ink", "blue", "red", "amber", "green")


@dataclass(frozen=True)
class Slides:
    """The opening and closing slides' colours and hand (`frame.py`)."""

    paper: str = "#fbfaf6"
    ink: str = "#1f2937"
    muted: str = "#6b7280"
    soft: str = "#4b5563"
    accent: str = "#2563eb"
    card: str = "#fff"
    edge: str = "#1f2937"
    rule: str = "#d6d3cb"
    tick: str = "#16a34a"
    on_accent: str = "#fff"


@dataclass(frozen=True)
class Theme:
    id: str
    label: str
    family: Literal["drawn", "illustrated"] = "drawn"
    # The paper, and what is printed on it before anything is drawn.
    paper: RGB = (0xFB, 0xFA, 0xF6)
    background: Literal["plain", "lined", "grid", "slate", "newsprint", "card"] = "plain"
    # What is printed on it: the rules or grid, their spacing in px, and a
    # notebook's margin line.
    rule: RGB = (0xC7, 0xDB, 0xF5)
    spacing: int = 40
    margin: RGB | None = None
    # Each tone's colour.
    ink: dict[str, RGB] = field(
        default_factory=lambda: {
            "ink": (0x1F, 0x29, 0x37),
            "blue": (0x25, 0x63, 0xEB),
            "red": (0xDC, 0x26, 0x26),
            "amber": (0xD9, 0x77, 0x06),
            "green": (0x16, 0xA3, 0x4A),
        }
    )
    # The highlighter: multiplied on light paper, so the lines under it stay
    # dark; laid over, lighter, on dark paper.
    highlight: RGB = (0xFD, 0xE6, 0x8A)
    highlight_blend: Literal["multiply", "over"] = "multiply"
    highlight_alpha: float = 0.85
    # The pen: its width, how much a hand wobbles it (1 is the whiteboard's),
    # how its line is painted, and the tip that follows the drawing (its
    # point, collar and body).
    stroke: float = 5.0
    wobble: float = 1.0
    pen: Literal["marker", "ballpoint", "technical", "chalk", "print", "craft"] = "marker"
    # Shapes: a box or circle may be filled behind its label, with a halftone
    # screen (print) or solid cut paper (craft), in the tone's fill colour
    # (`fills`, else its ink); keep its outline or not; and cast a shadow.
    fill: Literal["none", "halftone", "solid"] = "none"
    fills: dict[str, RGB] = field(default_factory=dict[str, RGB])
    outline: bool = True
    shadow: bool = False
    # A second impression a little off the first, as a print run that is not
    # quite in register leaves; None for none.
    ghost: RGB | None = None
    tip: tuple[RGB, RGB, RGB] = ((0x1F, 0x29, 0x37), (0xE5, 0xE7, 0xEB), (0x25, 0x63, 0xEB))
    # The hand: a font under `assets/`, drawn as outlines.
    font: str = "Caveat-Bold.ttf"
    title_size: float = 64
    label_size: float = 50
    slides: Slides = field(default_factory=Slides)
    # An illustrated theme: how its pictures are painted (`illustrate.py`),
    # and the drawn theme a scene falls back to when its picture cannot be
    # made or does not pass its check.
    style: str = ""
    twin: str = "whiteboard"


WHITEBOARD = Theme(id="whiteboard", label="Whiteboard")

# A school notebook: lined paper with a red margin, a blue ballpoint, a yellow
# highlighter.
NOTEBOOK = Theme(
    id="notebook",
    label="Notebook",
    paper=(0xFF, 0xFE, 0xF8),
    background="lined",
    rule=(0xC9, 0xDC, 0xF2),
    spacing=40,
    margin=(0xF2, 0xA7, 0xA7),
    ink={
        "ink": (0x1E, 0x3A, 0x8A),
        "blue": (0x1D, 0x4E, 0xD8),
        "red": (0xB9, 0x1C, 0x1C),
        "amber": (0xB4, 0x53, 0x09),
        "green": (0x15, 0x80, 0x3D),
    },
    highlight=(0xFE, 0xF0, 0x8A),
    stroke=3.4,
    wobble=0.8,
    pen="ballpoint",
    tip=((0x1E, 0x3A, 0x8A), (0xD1, 0xD5, 0xDB), (0x1D, 0x4E, 0xD8)),
    font="Kalam-Bold.ttf",
    title_size=58,
    label_size=44,
    slides=Slides(
        paper="#fffef8", ink="#1e3a8a", muted="#64748b", soft="#334155", accent="#1d4ed8",
        card="#ffffff", edge="#1e3a8a", rule="#c9dcf2", tick="#15803d",
    ),
)  # fmt: skip

# A slate in a classroom: green-black board, chalk with grain, pastel chalks;
# a highlight is a smudge of chalk behind the words.
CHALKBOARD = Theme(
    id="chalkboard",
    label="Chalkboard",
    paper=(0x23, 0x3A, 0x32),
    background="slate",
    ink={
        "ink": (0xF2, 0xF0, 0xE8),
        "blue": (0x9C, 0xD3, 0xF5),
        "red": (0xF5, 0xA3, 0xA3),
        "amber": (0xF7, 0xD3, 0x7A),
        "green": (0xB5, 0xE8, 0xA8),
    },
    highlight=(0xF2, 0xF0, 0xE8),
    highlight_blend="over",
    highlight_alpha=0.14,
    stroke=6.5,
    wobble=1.25,
    pen="chalk",
    tip=((0xF2, 0xF0, 0xE8), (0xE6, 0xE1, 0xD3), (0xF5, 0xF2, 0xE9)),
    font="GochiHand-Regular.ttf",
    title_size=64,
    label_size=50,
    slides=Slides(
        paper="#233a32", ink="#f2f0e8", muted="#b9c6bf", soft="#d8e0db", accent="#f7d37a",
        card="#2b453c", edge="#f2f0e8", rule="#4a6359", tick="#b5e8a8", on_accent="#233a32",
    ),
)  # fmt: skip

# An engineer's blueprint: Prussian blue sheet, white grid, a thin even
# technical pen, a drafting hand.
BLUEPRINT = Theme(
    id="blueprint",
    label="Blueprint",
    paper=(0x14, 0x3D, 0x7A),
    background="grid",
    rule=(0x2A, 0x58, 0x96),
    spacing=48,
    ink={
        "ink": (0xF0, 0xF6, 0xFF),
        "blue": (0xA8, 0xD8, 0xFF),
        "red": (0xFF, 0xB8, 0xAC),
        "amber": (0xFF, 0xE0, 0x8A),
        "green": (0xB8, 0xF0, 0xC8),
    },
    highlight=(0xFF, 0xFF, 0xFF),
    highlight_blend="over",
    highlight_alpha=0.13,
    stroke=2.8,
    wobble=0.35,
    pen="technical",
    tip=((0xF0, 0xF6, 0xFF), (0x9F, 0xB3, 0xCC), (0x1F, 0x29, 0x37)),
    font="ArchitectsDaughter-Regular.ttf",
    title_size=56,
    label_size=42,
    slides=Slides(
        paper="#143d7a", ink="#f0f6ff", muted="#a9c1e0", soft="#d2e2f5", accent="#ffe08a",
        card="#174684", edge="#f0f6ff", rule="#3c68a3", tick="#b8f0c8", on_accent="#143d7a",
    ),
)  # fmt: skip

# A mid-century print: cream newsprint, a few spot inks, halftone screens,
# a second impression slightly out of register, a typewriter face.
RETRO_PRINT = Theme(
    id="retro",
    label="Retro Print",
    paper=(0xF3, 0xEA, 0xD3),
    background="newsprint",
    ink={
        "ink": (0x1C, 0x1B, 0x1A),
        "blue": (0x1F, 0x3A, 0x5F),
        "red": (0xA8, 0x23, 0x1C),
        "amber": (0x9A, 0x3F, 0x12),
        "green": (0x2A, 0x55, 0x48),
    },
    highlight=(0xF2, 0xC1, 0x4E),
    highlight_alpha=0.6,
    stroke=4.2,
    wobble=0.6,
    pen="print",
    fill="halftone",
    ghost=(0xC8, 0x3A, 0x30),
    tip=((0x1C, 0x1B, 0x1A), (0xCF, 0xC3, 0xA3), (0xA8, 0x23, 0x1C)),
    font="SpecialElite-Regular.ttf",
    title_size=56,
    label_size=38,
    slides=Slides(
        paper="#f3ead3", ink="#1c1b1a", muted="#6b6255", soft="#3d372f", accent="#a8231c",
        card="#fbf6e9", edge="#1c1b1a", rule="#cfc3a3", tick="#2a5548",
    ),
)  # fmt: skip

# Cut paper on card: solid pastel shapes with soft shadows, labels in dark
# ink, arrows as strips of paper.
PAPER_CRAFT = Theme(
    id="papercraft",
    label="Paper-craft",
    paper=(0xEF, 0xE7, 0xDA),
    background="card",
    ink={
        "ink": (0x1F, 0x29, 0x37),
        "blue": (0x1E, 0x3A, 0x8A),
        "red": (0x99, 0x1B, 0x1B),
        "amber": (0x7C, 0x2D, 0x12),
        "green": (0x14, 0x53, 0x2D),
    },
    fills={
        "ink": (0xFF, 0xFD, 0xF8),
        "blue": (0xBF, 0xDB, 0xFE),
        "red": (0xFE, 0xCA, 0xCA),
        "amber": (0xFD, 0xE6, 0x8A),
        "green": (0xBB, 0xF7, 0xD0),
    },
    highlight=(0xFD, 0xE6, 0x8A),
    highlight_alpha=0.7,
    stroke=7.0,
    wobble=1.6,
    pen="craft",
    fill="solid",
    outline=False,
    shadow=True,
    tip=((0x1F, 0x29, 0x37), (0xD8, 0xCD, 0xB9), (0xC2, 0x41, 0x0C)),
    font="PatrickHand-Regular.ttf",
    title_size=64,
    label_size=48,
    slides=Slides(
        paper="#efe7da", ink="#1f2937", muted="#6b6255", soft="#3d372f", accent="#c2410c",
        card="#fffdf8", edge="#1f2937", rule="#d8cdb9", tick="#14532d",
    ),
)  # fmt: skip

# ── illustrated: a picture per scene, under the studio's own labels ─────────
#
# The picture is painted by an image model in the theme's style, from the
# scene's brief and concepts, never its words; the labels are the studio's,
# checked as a drawn scene's are, set on cards over the picture. The paper and
# inks are the cards' and the slides'.


def _illustrated(
    id: str, label: str, style: str, twin: str, paper: RGB, accent: str,
    ink: dict[str, RGB] | None = None, highlight: RGB | None = None,
) -> Theme:  # fmt: skip
    base = THEMES_DRAWN[twin]
    return dataclasses.replace(
        base, id=id, label=label, family="illustrated", style=style, twin=twin, paper=paper,
        background="plain", pen="craft", fill="none", outline=True, shadow=False, ghost=None,
        ink=ink or base.ink, highlight=highlight or base.highlight,
        slides=dataclasses.replace(base.slides, accent=accent),
    )  # fmt: skip


THEMES_DRAWN: dict[str, Theme] = {
    t.id: t for t in (WHITEBOARD, NOTEBOOK, CHALKBOARD, BLUEPRINT, RETRO_PRINT, PAPER_CRAFT)
}

WATERCOLOR = _illustrated(
    "watercolor", "Watercolor",
    "Soft watercolour washes on textured cold-press paper, loose ink outlines, muted natural "
    "palette, gentle light, lots of white space.",
    "notebook", (0xFB, 0xF8, 0xF1), "#3b6ea8",
)  # fmt: skip
ANIME = _illustrated(
    "anime", "Anime",
    "Clean cel-shaded anime illustration, crisp line art, bright but soft colours, simple "
    "backgrounds, a friendly educational tone.",
    "whiteboard", (0xF7, 0xF8, 0xFC), "#e0457b",
    # The whiteboard's amber and green are too light for words on a card.
    ink={
        "ink": (0x1F, 0x29, 0x37), "blue": (0x1D, 0x4E, 0xD8), "red": (0xB9, 0x1C, 0x1C),
        "amber": (0xB4, 0x53, 0x09), "green": (0x15, 0x80, 0x3D),
    },
    highlight=(0xFE, 0xF0, 0x8A),
)  # fmt: skip
HERITAGE = _illustrated(
    "heritage", "Heritage",
    "A vintage engraved textbook plate: fine cross-hatched lines, sepia ink on aged cream "
    "paper, careful scientific illustration.",
    "retro", (0xF3, 0xEA, 0xD3), "#8a5a2b",
)  # fmt: skip
KAWAII = _illustrated(
    "kawaii", "Kawaii",
    "Cute kawaii illustration: rounded friendly shapes, pastel colours, simple smiling "
    "characters, soft shading, a calm plain background.",
    "papercraft", (0xFD, 0xF6, 0xF9), "#d9467a",
)  # fmt: skip

THEMES: dict[str, Theme] = {
    **THEMES_DRAWN,
    **{t.id: t for t in (WATERCOLOR, ANIME, HERITAGE, KAWAII)},
}
DEFAULT = WHITEBOARD.id


def theme_of(theme_id: str | None) -> Theme:
    """The theme named, or the whiteboard for one this studio does not have
    (a video made before themes has none)."""
    return THEMES.get(theme_id or "", WHITEBOARD)


_current: ContextVar[Theme] = ContextVar("whiteboard_theme", default=WHITEBOARD)


def current() -> Theme:
    return _current.get()


@contextmanager
def using(t: Theme | str | None) -> Generator[Theme]:
    """Draw with `t` (a theme or its id) until the block ends."""
    th = t if isinstance(t, Theme) else theme_of(t)
    token = _current.set(th)
    try:
        yield th
    finally:
        _current.reset(token)
