"""Drawing a cover: a validated `Cover` into one self-contained HTML page.

Ported from `opennotebook_server/src/cover/render.rs`, number for number, so
a cover draws as it did.

A cover is drawn with the studio's own parts, so it reads as part of the app:
the page and its dot grid as on the mind map's canvas, the motif in a rounded
tile like a Create tile's glyph, the terms as chips, a small uppercase label,
and the title in the app's sans at 600. Every colour but one is a theme token
from `theme.css` (a copy of `web/src/styles/theme.css`), embedded in the page
and switched by `data-bs-theme` exactly as the app switches it, so a cover
follows the viewer's theme. The one colour of its own is the collection's
accent, on the motif's tile and the short rule before the label.

The page is a 1600x900 SVG with system fonts, no script and nothing fetched,
so the web app can frame it in a sandboxed iframe and scale it to a card,
about 0.16 to 0.19 of its size. Sizes, radii and hairlines are chosen for
that scale: a radius of 56 here is the 10 of an app card. Text is laid out
here, not by the browser: an SVG `<text>` never wraps, so every line is
measured with a per-glyph width estimate, the size is stepped down until the
text fits its box, and what still does not fit is cut with an ellipsis. The
estimates lean wide, so a line drawn in a narrower font than guessed only
leaves more margin.
"""

import math
import unicodedata
from dataclasses import dataclass
from enum import Enum
from functools import cache
from pathlib import Path

from opennotebook.cover import motifs

W = 1600.0
H = 900.0

_MASK = (1 << 64) - 1


@cache
def theme_css() -> str:
    """The token sheet: Bootstrap 5.3 names, dark by default, light under
    `data-bs-theme="light"`."""
    return (Path(__file__).parent / "theme.css").read_text(encoding="utf-8")


class Theme(Enum):
    """The theme a cover is drawn in: the app's own, dark unless the viewer
    chose light."""

    DARK = "dark"
    LIGHT = "light"

    @classmethod
    def parse(cls, s: str) -> Theme:
        """`light` is light; anything else is the studio's default."""
        return cls.LIGHT if s.strip().lower() == "light" else cls.DARK


@dataclass(frozen=True)
class Accent:
    """A collection's one colour. The same in both themes, as the app's
    primary is: white reads on it at 4.5:1, and it stands at 3:1 against the
    page and the card surface of either theme; the tests hold every accent to
    that."""

    id: str
    # What the prompt tells the model it suits.
    mood: str
    fill: str
    # The motif on the fill.
    on_fill: str


ACCENTS: tuple[Accent, ...] = (
    Accent(
        "blue",
        "the studio's own blue, for science, technology and anything general",
        "#2563eb",
        "#ffffff",
    ),
    Accent("teal", "for the sea, climate, data, medicine", "#0f7a72", "#ffffff"),
    Accent("green", "for nature, biology, ecology, health", "#15803d", "#ffffff"),
    Accent("amber", "for history, economics, finance, craft", "#a16207", "#ffffff"),
    Accent("coral", "for geography, travel, food, culture", "#c2410c", "#ffffff"),
    Accent("rose", "for the arts, music, people, literature", "#d42a5c", "#ffffff"),
    Accent("violet", "for ideas, philosophy, language, space", "#7c4dee", "#ffffff"),
    Accent("slate", "for computing, engineering, mathematics, law", "#64748b", "#ffffff"),
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

# The three arrangements, with what the prompt says each suits.
LAYOUTS: tuple[tuple[str, str], ...] = (
    ("tile", "the symbol in its tile above a large topic, for most subjects"),
    (
        "card",
        "the topic on a card with the symbol and the terms, for a subject with a few clear parts",
    ),
    (
        "map",
        "the symbol as the root of a small map of the terms, for a subject of connected ideas",
    ),
)

# The first layouts, each read as the one nearest it.
OLD_LAYOUTS: tuple[tuple[str, str], ...] = (
    ("emblem", "card"),
    ("editorial", "tile"),
    ("grid", "map"),
    ("horizon", "tile"),
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
    terms: tuple[str, ...]
    motif: str
    accent: Accent
    layout: str
    theme: Theme
    # Places the tile and the grid, so two covers alike still differ.
    seed: int


# ── text ──────────────────────────────────────────────────────────────────────

# The kicker over every topic, as a section label sets it.
KICKER = "COLLECTION"


class Face(Enum):
    # A title: 600, the heaviest the studio sets.
    TITLE = "t"
    # A chip's words: 500.
    CHIP = "c"

    @property
    def factor(self) -> float:
        """How much wider than the estimate this face sets, at most: measured
        against DejaVu Sans at 600 and 500, the widest of the stack."""
        return 1.18 if self is Face.TITLE else 1.08


_NARROW = set("ilj'|!.,:;·")
_SLIM = set("ftrI()[]-")


def em(c: str) -> float:
    """A glyph's advance in ems for a bold sans, rounded up: wide enough for
    Segoe UI, Roboto, Helvetica and Inter alike. Wider faces, such as the
    DejaVu Sans many Linux desktops resolve `system-ui` to, are covered by
    each `Face`'s factor."""
    if c == " ":
        return 0.28
    if c in _NARROW:
        return 0.3
    if c in _SLIM:
        return 0.4
    if c in "mw":
        return 0.9
    if c in "MW@%":
        return 0.98
    if c in "—…":
        return 1.0
    if "A" <= c <= "Z":
        return 0.72
    if "0" <= c <= "9":
        return 0.6
    if c.isascii():
        return 0.58
    # CJK, kana, hangul and other full-width scripts.
    if "ᄀ" <= c <= "￯" and not (" " <= c <= "⹿"):
        return 1.04
    return 0.66


def width(s: str, size: float, face: Face) -> float:
    return sum(em(c) for c in s) * size * face.factor


def wrap(text: str, size: float, max_w: float, face: Face) -> list[str]:
    """Greedy word wrap; a word longer than a line is broken by characters,
    so a long compound or a line of CJK still fits."""
    lines: list[str] = []
    cur = ""
    for word in text.split():
        joined = word if not cur else f"{cur} {word}"
        if width(joined, size, face) <= max_w:
            cur = joined
            continue
        if cur:
            lines.append(cur)
            cur = ""
        if width(word, size, face) <= max_w:
            cur = word
            continue
        for c in word:
            nxt = cur + c
            if width(nxt, size, face) > max_w and cur:
                lines.append(cur)
                cur = c
            else:
                cur = nxt
    if cur:
        lines.append(cur)
    return lines


def clip_line(s: str, size: float, max_w: float, face: Face) -> str:
    """`s` cut to `max_w`, with an ellipsis when anything was cut."""
    if width(s, size, face) <= max_w:
        return s
    out = s
    while out and width(f"{out}…", size, face) > max_w:
        out = out[:-1]
    return f"{out.rstrip(' ,:;-·')}…"


@dataclass
class Fitted:
    """Text set in a box: the size it fits at and its lines."""

    size: float
    lines: list[str]


def fit(
    text: str,
    face: Face,
    max_w: float,
    max_h: float,
    max_lines: int,
    max: float,
    min: float,
) -> Fitted:
    """The largest size from `max` down to `min` at which `text` wraps into at
    most `max_lines` lines of `max_w` within `max_h`; at `min` the lines past
    the last are dropped and the last one ends in an ellipsis."""
    leading = 1.12
    size = max
    while True:
        lines = wrap(text, size, max_w, face)
        fits_h = len(lines) * size * leading <= max_h
        # A word broken across lines reads worse than a smaller size, so
        # breaking one is a last resort taken only at the smallest.
        whole = all(width(w, size, face) <= max_w for w in text.split())
        if len(lines) <= max_lines and fits_h and (whole or size <= min):
            return Fitted(size, lines)
        if size <= min:
            room = math.floor(max_h / (size * leading))
            room = 1 if room < 1 else room
            room = max_lines if room > max_lines else room
            kept = lines[:room]
            if len(lines) > room and kept:
                last = clip_line(f"{kept[-1]} …", size, max_w, face)
                if not last.endswith("…"):
                    last += "…"
                kept[-1] = last
            return Fitted(size, kept)
        size = size * 0.94
        size = min if size < min else size


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


def _n(v: float) -> str:
    """A number as the Rust renderer printed it with `{:.0}`."""
    return f"{v:.0f}"


def title(svg: list[str], f: Fitted, x: float, y: float) -> None:
    """Lines of a title, the first baseline at `y`."""
    lh = f.size * 1.12
    for i, line in enumerate(f.lines):
        svg.append(
            f'<text class="{Face.TITLE.value}" x="{_n(x)}" y="{_n(y + i * lh)}" '
            f'font-size="{_n(f.size)}">{esc(line)}</text>'
        )


def block_h(f: Fitted) -> float:
    """The height a fitted block takes, cap height of the first line to the
    baseline of the last."""
    return f.size * 0.74 + max(len(f.lines) - 1, 0) * f.size * 1.12


# ── parts ─────────────────────────────────────────────────────────────────────


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

    def coin(self) -> bool:
        return self.next() & 1 == 1


# The margin on every side: a card's inner padding at card size.
PAD = 112.0
# A hairline: about one pixel at card size.
LINE = 4.0
# The dot grid's pitch; it reads as the map canvas's at card size.
GRID = 56.0
# Radii: the 4 of a chip, the 10 of a card, at card size.
R_CHIP = 22.0
R_CARD = 56.0
CHIP_H = 92.0
CHIP_TEXT = 48.0
CHIP_PAD = 34.0
CHIP_GAP = 20.0
KICKER_TEXT = 38.0


def tile(svg: list[str], name: str, x: float, y: float, size: float, a: Accent) -> None:
    """The motif's tile: the accent, rounded as a Create tile's glyph is (9 of
    32), the motif half its size on it."""
    svg.append(
        f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(size)}" height="{_n(size)}" '
        f'rx="{_n(size * 9.0 / 32.0)}" fill="{a.fill}"/>'
    )
    inner = motifs.motif(name) or motifs.motif(motifs.FALLBACK) or ""
    g = size * 0.5
    svg.append(
        f'<g transform="translate({x + (size - g) / 2.0:.1f} {y + (size - g) / 2.0:.1f}) '
        f'scale({g / 16.0:.3f})" fill="{a.on_fill}">{inner}</g>'
    )


def kicker(svg: list[str], x: float, y: float, a: Accent) -> None:
    """The short accent rule and the section label, its baseline at `y`."""
    svg.append(
        f'<rect x="{_n(x)}" y="{_n(y - KICKER_TEXT * 0.37 - 4.0)}" width="48" height="8" '
        f'rx="4" fill="{a.fill}"/><text class="k" x="{_n(x + 72.0)}" y="{_n(y)}" '
        f'font-size="{_n(KICKER_TEXT)}">{KICKER}</text>'
    )


def chip_row(
    svg: list[str], terms: tuple[str, ...], x: float, y: float, max_w: float, max_chip: float
) -> int:
    """The terms that fit whole on one row from (`x`, `y`) within `max_w`,
    each chip at most `max_chip` wide; the first is cut rather than left off,
    so a row with terms is never empty. Returns how many were drawn."""
    at = x
    n = 0
    for t in terms:
        w = chip(svg, t, at, y, min(x + max_w - at, max_chip), n == 0)
        if w is None:
            continue
        at += w + CHIP_GAP
        n += 1
    return n


def chip(svg: list[str], term: str, x: float, y: float, room: float, force: bool) -> float | None:
    """One chip at (`x`, `y`) no wider than `room`, its width. A term that
    does not fit whole is left off, unless `force`: then it is cut to the
    room, if that leaves a few letters."""
    text_room = room - 2.0 * CHIP_PAD
    whole = width(term, CHIP_TEXT, Face.CHIP)
    if whole > text_room and (not force or text_room < 3.0 * CHIP_TEXT):
        return None
    line = clip_line(term, CHIP_TEXT, text_room, Face.CHIP)
    w = width(line, CHIP_TEXT, Face.CHIP) + 2.0 * CHIP_PAD
    svg.append(
        f'<rect class="chip" x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(CHIP_H)}" '
        f'rx="{_n(R_CHIP)}" stroke-width="{_n(LINE)}"/><text class="c" x="{_n(x + CHIP_PAD)}" '
        f'y="{_n(y + CHIP_H / 2.0 + CHIP_TEXT * 0.36)}" font-size="{_n(CHIP_TEXT)}">'
        f"{esc(line)}</text>"
    )
    return w


# ── layouts ───────────────────────────────────────────────────────────────────


def tile_layout(svg: list[str], c: Cover, rng: Rng) -> None:
    """A large motif tile on one side, centred; on the other the label, the
    topic and a row of chips, centred as a group."""
    size = 256.0
    gap = 88.0
    left = rng.coin()
    tx = PAD if left else W - PAD - size
    tile(svg, c.motif, tx, (H - size) / 2.0, size, c.accent)

    x = PAD + size + gap if left else PAD
    col = W - 2.0 * PAD - size - gap
    n = chip_row([], c.terms, 0.0, 0.0, col, 560.0)
    chips_h = 56.0 + CHIP_H if n > 0 else 0.0
    head = KICKER_TEXT * 0.74 + 48.0
    room = H - 2.0 * PAD - head - chips_h
    topic = fit(c.topic, Face.TITLE, col, room, 3, 124.0, 80.0)
    group = head + block_h(topic) + chips_h
    top = (H - group) / 2.0
    kicker(svg, x, top + KICKER_TEXT * 0.74, c.accent)
    title(svg, topic, x - 4.0, top + head + topic.size * 0.74)
    if n > 0:
        y = top + head + block_h(topic) + 56.0
        chip_row(svg, c.terms, x, y, col, 560.0)


def card_layout(svg: list[str], c: Cover, rng: Rng) -> None:
    """A card floating on the canvas, as tall as what it holds: the tile and
    the label as its header, the topic, a hairline, and the chips."""
    x0, x1 = 64.0, W - 64.0
    inset = 72.0
    cx0, cx1 = x0 + inset, x1 - inset
    size = 168.0
    n = chip_row([], c.terms, 0.0, 0.0, cx1 - cx0, 560.0)
    foot = 44.0 + 40.0 + CHIP_H if n > 0 else 0.0
    room = H - 2.0 * 64.0 - 2.0 * inset - size - 48.0 - foot
    topic = fit(c.topic, Face.TITLE, cx1 - cx0, room, 2, 112.0, 80.0)
    h = 2.0 * inset + size + 48.0 + block_h(topic) + foot
    y0 = (H - h) / 2.0
    svg.append(
        f'<rect class="panel" x="{_n(x0)}" y="{_n(y0)}" width="{_n(x1 - x0)}" height="{_n(h)}" '
        f'rx="{_n(R_CARD)}" stroke-width="{_n(LINE)}"/>'
    )
    cy0 = y0 + inset
    # The tile leads the header on the left or closes it on the right.
    left = rng.coin()
    tx = cx0 if left else cx1 - size
    tile(svg, c.motif, tx, cy0, size, c.accent)
    kx = cx0 + size + 48.0 if left else cx0
    kicker(svg, kx, cy0 + size / 2.0 + KICKER_TEXT * 0.36, c.accent)
    first = cy0 + size + 48.0 + topic.size * 0.74
    title(svg, topic, cx0 - 4.0, first)
    if n > 0:
        rule = first - topic.size * 0.74 + block_h(topic) + 44.0
        svg.append(
            f'<line class="rule" x1="{_n(cx0)}" y1="{_n(rule)}" x2="{_n(cx1)}" y2="{_n(rule)}" '
            f'stroke-width="{_n(LINE)}"/>'
        )
        chip_row(svg, c.terms, cx0, rule + 40.0, cx1 - cx0, 560.0)


def map_layout(svg: list[str], c: Cover, rng: Rng) -> None:
    """The label and the topic at the top; under them the motif's tile as the
    root of a small map, its links running to the terms as the map draws
    them."""
    top = PAD + KICKER_TEXT * 0.74
    kicker(svg, PAD, top, c.accent)
    topic = fit(c.topic, Face.TITLE, W - 2.0 * PAD, 236.0, 2, 112.0, 80.0)
    title(svg, topic, PAD - 4.0, top + 44.0 + topic.size * 0.74)
    map_top = top + 44.0 + block_h(topic) + 64.0

    bottom = H - PAD
    size = min(200.0, bottom - map_top)
    ry = map_top + (bottom - map_top - size) / 2.0
    mid = ry + size / 2.0
    shown = c.terms[:3]
    if shown:
        gap = 24.0
        h = len(shown) * CHIP_H + (len(shown) - 1) * gap
        first = mid - h / 2.0
        links: list[str] = []
        chips: list[str] = []
        for i, t in enumerate(shown):
            y = first + i * (CHIP_H + gap)
            x = 560.0 + rng.range(0.0, 72.0)
            if chip(chips, t, x, y, W - PAD - x, True) is None:
                continue
            x1, y1, x2, y2 = PAD + size, mid, x, y + CHIP_H / 2.0
            k = (x2 - x1) / 2.0
            links.append(
                f'<path class="link" d="M{_n(x1)} {_n(y1)}C{_n(x1 + k)} {_n(y1)} {_n(x2 - k)} '
                f'{_n(y2)} {_n(x2)} {_n(y2)}" stroke-width="{_n(LINE)}"/>'
            )
        svg.extend(links)
        svg.extend(chips)
    tile(svg, c.motif, PAD, ry, size, c.accent)


# The tokens a cover draws with, from the sheet the app itself uses.
STYLE = (
    "html,body{margin:0;height:100%;overflow:hidden;background:var(--bs-body-bg)}"
    "svg{display:block}"
    "text{font-family:var(--bs-font-sans-serif);font-kerning:normal}"
    ".bg{fill:var(--bs-body-bg)}"
    ".dot{fill:var(--bs-border-color)}"
    ".panel{fill:var(--bs-secondary-bg);stroke:var(--bs-border-color)}"
    ".chip{fill:var(--bs-secondary-bg);stroke:var(--bs-border-color)}"
    ".card .chip{fill:var(--bs-body-bg)}"
    ".rule{stroke:var(--bs-border-color-translucent)}"
    ".link{fill:none;stroke:var(--st-mm-link)}"
    ".t{fill:var(--bs-emphasis-color);font-weight:600;letter-spacing:-.01em}"
    ".k{fill:var(--bs-secondary-color);font-weight:600;letter-spacing:.07em}"
    ".c{fill:var(--bs-body-color);font-weight:500}"
)


def page(c: Cover) -> str:
    """The page: `<!doctype html>`, a CSP that allows nothing to load, the
    studio's theme, and the SVG filling the viewport."""
    rng = Rng(c.seed)
    svg: list[str] = []
    # The grid starts at a different point per collection.
    ox, oy = rng.range(0.0, GRID), rng.range(0.0, GRID)
    h = GRID / 2.0
    svg.append(
        f'<defs><pattern id="dots" width="{_n(GRID)}" height="{_n(GRID)}" '
        f'patternUnits="userSpaceOnUse" patternTransform="translate({_n(ox)} {_n(oy)})">'
        f'<circle class="dot" cx="{_n(h)}" cy="{_n(h)}" r="3.5"/></pattern></defs>'
        f'<rect class="bg" width="{W:g}" height="{H:g}"/>'
        f'<rect width="{W:g}" height="{H:g}" fill="url(#dots)"/>'
    )
    cls = layout(c.layout) or LAYOUTS[0][0]
    svg.append(f'<g class="{cls}">')
    if cls == "card":
        card_layout(svg, c, rng)
    elif cls == "map":
        map_layout(svg, c, rng)
    else:
        tile_layout(svg, c, rng)
    svg.append("</g>")
    theme = ' data-bs-theme="light"' if c.theme is Theme.LIGHT else ""
    t = esc(c.topic)
    return (
        f'<!doctype html><html{theme}><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta http-equiv="Content-Security-Policy" '
        "content=\"default-src 'none'; style-src 'unsafe-inline'; img-src data:\">"
        f"<title>{t}</title><style>{theme_css()}{STYLE}</style></head><body>"
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1600 900" width="100%" '
        'height="100%" preserveAspectRatio="xMidYMid slice" role="img" '
        f'aria-label="{t}">{"".join(svg)}</svg></body></html>'
    )
