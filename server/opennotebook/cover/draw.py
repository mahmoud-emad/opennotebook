"""Drawing a cover: a validated `Cover` into one self-contained HTML page.

A cover is a picture, not a label: the card it sits on already prints the
collection's name, so a cover carries no words. It is the collection's one
colour as a field of light, its motif drawn large enough to read on a 72 px
row, and a fine grain, so the colour reads as a surface rather than a flat
fill. The same picture serves both themes, as a book's cover does.

Two arrangements, both from the seed so two covers alike still differ:

- `aura`: soft light over the accent's deep ground, the motif in a glass
  tile at the centre with a blurred echo of it behind;
- `relief`: the motif pressed large into the cover, as a stamp in cloth, a
  small foil mark in the corner.

The page is 1600x900 CSS pixels with inline styles and inline SVG only, no
script, no font and nothing fetched, so the web app can frame it in a
sandboxed iframe and scale it to a card, about 0.05 to 0.23 of its size.
Sizes are chosen for that scale: the motif's tile is a quarter of the width,
and the grain's frequency is set so it reads at card size.
"""

import unicodedata
from dataclasses import dataclass
from enum import Enum

from opennotebook.cover import motifs

W = 1600.0
H = 900.0

_MASK = (1 << 64) - 1


class Theme(Enum):
    """The theme a cover was asked for. A cover looks the same in both; the
    page names the theme it was served for, so a cached copy is one theme's."""

    DARK = "dark"
    LIGHT = "light"

    @classmethod
    def parse(cls, s: str) -> Theme:
        """`light` is light; anything else is the studio's default."""
        return cls.LIGHT if s.strip().lower() == "light" else cls.DARK


@dataclass(frozen=True)
class Accent:
    """A collection's one colour, as a tonal ramp from its ground to its
    light. `fill` is the colour itself, the studio's primary for blue; the
    glow is turned a little round the wheel, so the light reads as light and
    not as a lighter tint. White reads on `base` and the glow stands on it;
    the tests hold every accent to that."""

    id: str
    # What the prompt tells the model it suits.
    mood: str
    fill: str
    deep: str
    base: str
    bright: str
    glow: str


ACCENTS: tuple[Accent, ...] = (
    Accent(
        "blue",
        "the studio's own blue, for science, technology and anything general",
        "#2563eb",
        "#08112b",
        "#1b3a9c",
        "#3d6df5",
        "#7fd0ff",
    ),
    Accent(
        "teal",
        "for the sea, climate, data, medicine",
        "#0f7a72",
        "#031a1a",
        "#0b5f59",
        "#14a08f",
        "#9cf0cf",
    ),
    Accent(
        "green",
        "for nature, biology, ecology, health",
        "#15803d",
        "#05170d",
        "#145f32",
        "#2ca25a",
        "#d3ee7a",
    ),
    Accent(
        "amber",
        "for history, economics, finance, craft",
        "#a16207",
        "#1c1003",
        "#7a4407",
        "#d1830f",
        "#ffd889",
    ),
    Accent(
        "coral",
        "for geography, travel, food, culture",
        "#c2410c",
        "#1f0c04",
        "#8f300a",
        "#e0571a",
        "#ffc07a",
    ),
    Accent(
        "rose",
        "for the arts, music, people, literature",
        "#d42a5c",
        "#22040f",
        "#941d44",
        "#e0466d",
        "#ffae8f",
    ),
    Accent(
        "violet",
        "for ideas, philosophy, language, space",
        "#7c4dee",
        "#0f0828",
        "#4a2bb3",
        "#8562fb",
        "#f2a6ff",
    ),
    Accent(
        "slate",
        "for computing, engineering, mathematics, law",
        "#64748b",
        "#0b0f16",
        "#2f3b4f",
        "#5b6b85",
        "#b4c6ff",
    ),
)

# The palettes covers were first designed with, each read as the accent
# nearest it, so a stored spec or an old answer still draws.
OLD_PALETTES: tuple[tuple[str, str], ...] = (
    ("paper", "coral"),
    ("sage", "green"),
    ("sky", "blue"),
    ("blush", "rose"),
    ("lilac", "violet"),
    ("sand", "amber"),
    ("ocean", "teal"),
    ("dusk", "violet"),
    ("forest", "green"),
    ("graphite", "slate"),
)

# The two arrangements, with what the prompt says each suits.
LAYOUTS: tuple[tuple[str, str], ...] = (
    ("aura", "the symbol in a glass tile on a field of light, for most subjects"),
    (
        "relief",
        "the symbol pressed large into the cover, for history, craft, law and anything classic",
    ),
)

# Layouts covers were designed with before, each read as the one nearest it.
OLD_LAYOUTS: tuple[tuple[str, str], ...] = (
    ("tile", "aura"),
    ("card", "relief"),
    ("map", "aura"),
    ("emblem", "relief"),
    ("editorial", "aura"),
    ("grid", "aura"),
    ("horizon", "aura"),
)


def accent(id: str) -> Accent | None:
    """The accent an id names, an old palette id included."""
    id = dict(OLD_PALETTES).get(id, id)
    return next((a for a in ACCENTS if a.id == id), None)


def layout(id: str) -> str | None:
    """The layout an id names, an old layout id included."""
    id = dict(OLD_LAYOUTS).get(id, id)
    return next((name for name, _ in LAYOUTS if name == id), None)


@dataclass(frozen=True)
class Cover:
    """A cover ready to draw: every field already checked against the lists."""

    topic: str
    motif: str
    accent: Accent
    layout: str
    theme: Theme
    # Places the light and turns the motif, so two covers alike still differ.
    seed: int


def esc(s: str) -> str:
    out: list[str] = []
    for c in s:
        if c == "&":
            out.append("&amp;")
        elif c == "<":
            out.append("&lt;")
        elif c == ">":
            out.append("&gt;")
        elif c == '"':
            out.append("&quot;")
        elif c == "'":
            out.append("&#39;")
        elif unicodedata.category(c) == "Cc":
            continue
        else:
            out.append(c)
    return "".join(out)


class Rng:
    """Deterministic numbers from the seed: splitmix64."""

    def __init__(self, seed: int) -> None:
        self.state = seed & _MASK

    def next(self) -> int:
        self.state = (self.state + 0x9E3779B97F4A7C15) & _MASK
        z = self.state
        z = ((z ^ (z >> 30)) * 0xBF58476D1CE4E5B9) & _MASK
        z = ((z ^ (z >> 27)) * 0x94D049BB133111EB) & _MASK
        return z ^ (z >> 31)

    def range(self, lo: float, hi: float) -> float:
        """A number in `[lo, hi)`."""
        return lo + (self.next() >> 11) / float(1 << 53) * (hi - lo)


def _n(v: float) -> str:
    return f"{v:.1f}".removesuffix(".0")


def glyph(name: str, style: str) -> str:
    """The motif as an SVG of its own, sized and coloured by `style`."""
    inner = motifs.motif(name) or motifs.motif(motifs.FALLBACK) or ""
    return f'<svg viewBox="0 0 16 16" style="{style}" aria-hidden="true">{inner}</svg>'


def grain(freq: str, opacity: float) -> str:
    """Fine noise laid over the colour, so it reads as a surface."""
    return (
        f'<svg class="grain" style="opacity:{opacity}" aria-hidden="true"><filter id="grain">'
        f'<feTurbulence type="fractalNoise" baseFrequency="{freq}" numOctaves="2" '
        'stitchTiles="stitch"/><feColorMatrix type="saturate" values="0"/></filter>'
        '<rect width="100%" height="100%" filter="url(#grain)"/></svg>'
    )


# ── layouts ───────────────────────────────────────────────────────────────────


def aura(c: Cover, rng: Rng) -> tuple[str, str]:
    """Three glows over the deep ground, the motif's blurred echo, and the
    motif in a glass tile at the centre. Returns (ground, parts)."""
    a = c.accent
    gx, gy = rng.range(55, 95), rng.range(0, 35)
    bx, by = rng.range(0, 45), rng.range(55, 100)
    cx, cy = rng.range(30, 70), rng.range(30, 70)
    ground = (
        f"radial-gradient(1200px 960px at {_n(gx)}% {_n(gy)}%, {a.glow}d9 0%, {a.glow}00 62%),"
        f"radial-gradient(1440px 1200px at {_n(bx)}% {_n(by)}%, {a.bright}e6 0%, "
        f"{a.bright}00 64%),"
        f"radial-gradient(2080px 1600px at {_n(cx)}% {_n(cy)}%, {a.base} 0%, {a.base}00 72%),"
        f"{a.deep}"
    )
    turn = rng.range(-14, 14)
    dx, dy = rng.range(-96, 96), rng.range(-64, 64)
    echo = glyph(
        c.motif,
        "position:absolute;left:304px;top:-46px;width:992px;height:992px;"
        f"fill:{a.glow};opacity:.22;filter:blur(26px);"
        f"transform:translate({_n(dx)}px,{_n(dy)}px) rotate({_n(turn)}deg)",
    )
    tile = (
        '<div class="glass">'
        + glyph(
            c.motif,
            "width:192px;height:192px;fill:#fff;filter:drop-shadow(0 5px 10px rgba(0,0,0,.28))",
        )
        + "</div>"
    )
    return ground, echo + tile + grain("0.18", 0.4)


def relief(c: Cover, rng: Rng) -> tuple[str, str]:
    """The motif pressed into the accent: lit on its lower edge, shaded on
    its upper, a small foil mark beside. Returns (ground, parts)."""
    a = c.accent
    ground = f"radial-gradient(1920px 1440px at 18% 0%, {a.bright}66 0%, {a.bright}00 60%),{a.base}"
    x, y, turn = rng.range(736, 832), rng.range(-128, -32), rng.range(-12, 12)

    def at(d: float, paint: str) -> str:
        return glyph(
            c.motif,
            f"position:absolute;left:{_n(x)}px;top:{_n(y)}px;width:1056px;height:1056px;"
            f"transform:translate({_n(d)}px,{_n(d)}px) rotate({_n(turn)}deg);{paint}",
        )

    pressed = (
        at(5.6, "fill:rgba(255,255,255,.16)")
        + at(-5.6, "fill:rgba(0,0,0,.34)")
        + at(0, f"fill:{a.base};filter:brightness(.96)")
    )
    foil = (
        '<div class="foil">'
        + glyph(c.motif, f"width:74px;height:74px;fill:{a.glow}")
        + f'<i style="background:{a.glow}"></i></div>'
    )
    return ground, pressed + foil + grain("0.17 0.015", 0.55)


STYLE = (
    "html,body{margin:0;width:1600px;height:900px;overflow:hidden}"
    ".c{position:relative;width:1600px;height:900px;overflow:hidden;isolation:isolate}"
    ".c>svg{display:block}"
    ".glass{position:absolute;left:608px;top:258px;width:384px;height:384px;"
    "box-sizing:border-box;border-radius:23%;display:grid;place-items:center;"
    "background:rgba(255,255,255,.14);border:3px solid rgba(255,255,255,.32);"
    "box-shadow:0 35px 96px rgba(0,0,0,.28),inset 0 3px 0 rgba(255,255,255,.35);"
    "backdrop-filter:blur(38px) saturate(140%);-webkit-backdrop-filter:blur(38px) saturate(140%)}"
    ".foil{position:absolute;left:104px;bottom:104px;display:flex;align-items:center;gap:26px}"
    ".foil i{display:block;width:144px;height:5px;border-radius:3px;opacity:.8}"
    ".grain{position:absolute;inset:0;width:100%;height:100%;mix-blend-mode:overlay}"
)


def page(c: Cover) -> str:
    """The page: `<!doctype html>`, a CSP that allows nothing to load, and
    the cover filling its 1600x900 box."""
    rng = Rng(c.seed)
    ground, parts = relief(c, rng) if layout(c.layout) == "relief" else aura(c, rng)
    theme = ' data-bs-theme="light"' if c.theme is Theme.LIGHT else ""
    t = esc(c.topic)
    return (
        f'<!doctype html><html{theme}><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=1600">'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
        f"<title>{t}</title><style>{STYLE}</style></head><body>"
        f'<div class="c" role="img" aria-label="{t}" style="background:{ground}">{parts}</div>'
        "</body></html>"
    )
