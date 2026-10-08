"""Shapes as the pen draws them: SVG paths and glyphs turned into point
lists that can be revealed point by point, with a hand's slight wobble.

Every stroke is a polyline, sampled every `STEP` px along its length, so
drawing part of a stroke is drawing its first points and the pen's tip is
its last one. skia's own path measuring is not used for the reveal: in
skia-python 144 `PathMeasure.getSegment` replaces its destination instead
of adding to it, which drew one contour of an icon where it had several.

The wobble is seeded by the element, so the same scene draws the same way
on every render.
"""

# skia-python ships without complete type information: its objects are
# unknown to the type checker, which is said once here rather than at every use.
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

import itertools
import logging
import math
import os
import random
from dataclasses import dataclass
from functools import cache
from importlib import resources

import skia
from fontTools.pens.basePen import BasePen
from fontTools.svgLib.path import parse_path
from fontTools.ttLib import TTCollection, TTFont

from opennotebook.build.whiteboard import theme as th

# fontTools warns about harmless quirks in system fonts it opens for a
# missing letter ("extra bytes in post.stringData"); the worker's log is not
# the place for them.
logging.getLogger("fontTools").setLevel(logging.ERROR)

# Distance between sampled points, px. Small enough that a curve reads as a
# curve at 1080p, large enough that a 5-minute video stays cheap to draw.
STEP = 4.0

# A wobble's two waves, by their frequency per px of line, and how far in
# from each end it fades in, px.
SLOW_WAVE = (0.004, 0.009)
FAST_WAVE = (0.013, 0.025)
EASE_PX = 12

# A box's corner radius, px.
CORNER = 14
# An arrowhead's barbs: their length, px, and their angle off the shaft.
HEAD_PX = 22
HEAD_ANGLE = 0.45

# A space is this share of the size wide; so is a letter the font set
# turns out to lack.
SPACE = 0.3

type Point = tuple[float, float]
type Poly = list[Point]


class _SkiaPen(BasePen):  # pyright: ignore[reportMissingTypeArgument]
    """A fontTools pen that builds a skia path: it reads SVG path data
    (arcs included) and glyph outlines alike."""

    def __init__(self, glyph_set: object = None) -> None:
        super().__init__(glyph_set)  # pyright: ignore[reportArgumentType]
        self.path = skia.Path()

    def _moveTo(self, pt: Point) -> None:
        self.path.moveTo(*pt)

    def _lineTo(self, pt: Point) -> None:
        self.path.lineTo(*pt)

    def _curveToOne(self, pt1: Point, pt2: Point, pt3: Point) -> None:
        self.path.cubicTo(*pt1, *pt2, *pt3)

    def _qCurveToOne(self, pt1: Point, pt2: Point) -> None:
        self.path.quadTo(*pt1, *pt2)

    def _closePath(self) -> None:
        self.path.close()

    def _endPath(self) -> None:
        pass


def svg_path(ds: list[str]) -> skia.Path:
    """SVG path data, one or more `d` attributes, as one skia path."""
    pen = _SkiaPen()
    for d in ds:
        parse_path(d, pen)
    return pen.path


def polylines(path: skia.Path, step: float = STEP) -> list[Poly]:
    """Each contour of a path as points every `step` px along it."""
    out: list[Poly] = []
    m = skia.PathMeasure(path, False)
    while True:
        length = m.getLength()
        if length > 0:
            n = max(int(length / step), 1)
            pts: Poly = []
            for i in range(n + 1):
                pos, _ = m.getPosTan(length * i / n)
                pts.append((pos.x(), pos.y()))
            out.append(pts)
        if not m.nextContour():
            break
    return out


def path_of(points: Poly, closed: bool = False) -> skia.Path:
    """A polyline as a skia path, closed back to its start or not."""
    path = skia.Path()
    if points:
        path.moveTo(*points[0])
        for pt in points[1:]:
            path.lineTo(*pt)
        if closed:
            path.close()
    return path


def length_of(poly: Poly) -> float:
    return sum(math.dist(a, b) for a, b in itertools.pairwise(poly))


def wobble(poly: Poly, seed: int, amp: float = 1.3) -> Poly:
    """A polyline moved a little off its line, smoothly: two slow waves of
    seeded phase along its length, as a hand drifts. Its ends stay put, so
    joined strokes still meet."""
    if len(poly) < 3:
        return poly
    # The theme's hand: steadier for a technical pen, looser for chalk.
    amp *= th.current().wobble
    rnd = random.Random(seed)
    f1, f2 = rnd.uniform(*SLOW_WAVE), rnd.uniform(*FAST_WAVE)
    p1, p2 = rnd.uniform(0, math.tau), rnd.uniform(0, math.tau)
    total = length_of(poly) or 1.0
    out: Poly = []
    s = 0.0
    for i, (x, y) in enumerate(poly):
        if i:
            s += math.dist(poly[i - 1], (x, y))
        a, b = poly[max(i - 1, 0)], poly[min(i + 1, len(poly) - 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        norm = math.hypot(dx, dy) or 1.0
        ease = min(1.0, s / EASE_PX, (total - s) / EASE_PX)
        off = amp * ease * (0.7 * math.sin(f1 * s + p1) + 0.3 * math.sin(f2 * s + p2))
        out.append((x - dy / norm * off, y + dx / norm * off))
    return out


def transform(path: skia.Path, scale: float, dx: float, dy: float) -> skia.Path:
    """A copy of `path`, scaled about the origin and then moved."""
    m = skia.Matrix()
    m.setScale(scale, scale)
    m.postTranslate(dx, dy)
    out = skia.Path(path)
    out.transform(m)
    return out


def _drawn_round(p: skia.Path, overshoot: int, seed: int, amp: float) -> list[Poly]:
    """A closed shape drawn in one go, the pen running on past its start by
    `1 / overshoot` of the way round (two points at least)."""
    found = polylines(p)
    if not found:
        return []
    poly = found[0]
    over = poly[: max(len(poly) // overshoot, 2)]
    return [wobble(poly + over, seed, amp)]


def rough_rect(x: float, y: float, w: float, h: float, seed: int) -> list[Poly]:
    """A box drawn in one go, its corners slightly rounded, with a small
    overshoot where the pen comes back to its start."""
    p = skia.Path()
    p.addRoundRect(skia.Rect.MakeXYWH(x, y, w, h), CORNER, CORNER)
    return _drawn_round(p, 30, seed, 1.6)


def rough_ellipse(cx: float, cy: float, rx: float, ry: float, seed: int) -> list[Poly]:
    """An ellipse drawn in one go, overshooting its start a little more than
    a box does."""
    p = skia.Path()
    p.addOval(skia.Rect.MakeLTRB(cx - rx, cy - ry, cx + rx, cy + ry))
    return _drawn_round(p, 18, seed, 1.8)


def arrow(a: Point, b: Point, seed: int, head: bool = True, bend: float = 0.12) -> list[Poly]:
    """A gentle curve from `a` to `b`, and an arrowhead at `b`."""
    mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
    dx, dy = b[0] - a[0], b[1] - a[1]
    c = (mx - dy * bend, my + dx * bend)
    curve = skia.Path()
    curve.moveTo(*a)
    curve.quadTo(*c, *b)
    shaft = polylines(curve)
    if not shaft:
        # Its ends are one point: there is no arrow to draw.
        return []
    out = [wobble(shaft[0], seed, 1.0)]
    if head:
        ang = math.atan2(b[1] - c[1], b[0] - c[0])
        for side in (-1, 1):
            t = ang + math.pi - side * HEAD_ANGLE
            tip = (b[0] + HEAD_PX * math.cos(t), b[1] + HEAD_PX * math.sin(t))
            out += polylines(path_of([b, tip]))
    return out


# ── text ─────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Text:
    """A label's outlines, at the origin with its baseline at y = 0."""

    path: skia.Path
    width: float
    ascent: float
    descent: float
    # Letters no font here could draw, shown as "?".
    missing: tuple[str, ...] = ()


FALLBACK_KEY = "OPENNOTEBOOK_FALLBACK_FONTS"
# Where fonts are looked for when the handwriting font lacks a character:
# the setting (paths, separated by os.pathsep), then the usual places. A
# server image should carry Noto fonts for the languages it is used in.
FONT_DIRS = (
    "/usr/share/fonts",
    "/usr/local/share/fonts",
    "/System/Library/Fonts",
    "/Library/Fonts",
)


@cache
def _font_files() -> tuple[str, ...]:
    """Every font file to look in for a missing letter, the setting's first."""
    listed = [p for p in os.environ.get(FALLBACK_KEY, "").split(os.pathsep) if p.strip()]
    found: list[str] = []
    for d in FONT_DIRS:
        for root, _, names in os.walk(d):
            found += [os.path.join(root, n) for n in sorted(names)
                      if n.lower().endswith((".ttf", ".otf", ".ttc"))]  # fmt: skip
    return (*listed, *found)


def _has(path: str, cp: int) -> TTFont | None:
    """The font in `path` that has `cp`, opened, or None; every font looked
    at and not kept is closed."""
    try:
        if path.lower().endswith(".ttc"):
            coll = TTCollection(path, lazy=True)
            for i, f in enumerate(coll.fonts):
                if cp in (f.getBestCmap() or {}):
                    coll.close()
                    return TTFont(path, fontNumber=i, lazy=True)
            coll.close()
            return None
        f = TTFont(path, lazy=True)
        if cp in (f.getBestCmap() or {}):
            return f
        f.close()
    except Exception:
        return None
    return None


@cache
def fallback(ch: str) -> TTFont | None:
    """A font that has `ch`, for a letter the handwriting font lacks: Chinese,
    Japanese or Korean, say. It stays open for the process, as the handwriting
    font does. Scripts that need shaping or run right to left (Arabic,
    Hebrew, the Indic scripts) are not drawn by letter: without a shaping
    engine their letters would come out unjoined and backwards."""
    if _needs_shaping(ch):
        return None
    cp = ord(ch)
    for path in _font_files():
        found = _has(path, cp)
        if found is not None:
            return found
    return None


def _needs_shaping(ch: str) -> bool:
    """Whether `ch` belongs to a script whose letters join or reorder."""
    cp = ord(ch)
    return (
        0x0590 <= cp <= 0x08FF  # Hebrew, Arabic, Syriac, Thaana, NKo
        or 0x0900 <= cp <= 0x0DFF  # Devanagari to Sinhala
        or 0x0E00 <= cp <= 0x0FFF  # Thai, Lao, Tibetan
        or 0x1000 <= cp <= 0x109F  # Myanmar
        or 0xFB1D <= cp <= 0xFEFF  # presentation forms
    )


@cache
def _font(name: str) -> TTFont:
    """A hand under `assets/`, open for the process: each theme has one."""
    ref = resources.files("opennotebook.build.whiteboard") / "assets" / name
    with resources.as_file(ref) as f:
        return TTFont(str(f))


def text(s: str, size: float) -> Text:
    """`s` set in the handwriting font at `size` px, as filled outlines: the
    renderer draws no text of its own, so a label can never be lost to a
    missing font. A letter the handwriting font lacks comes from another
    font that has it (`fallback`); one no font can draw is a "?", and
    `missing` says which they were. The hand is the theme's."""
    font = _font(th.current().font)
    path = skia.Path()
    x = 0.0
    missing: list[str] = []
    for ch in s:
        if ch.isspace():
            x += size * SPACE
            continue
        use = font if ord(ch) in (font.getBestCmap() or {}) else fallback(ch)
        if use is None:
            missing.append(ch)
            use, ch = font, "?"
        x += _glyph(use, ch, size, x, path)
    return Text(path, x, *_metrics(font, size), missing=tuple(dict.fromkeys(missing)))


def _glyph(font: TTFont, ch: str, size: float, x: float, into: skia.Path) -> float:
    """Draw one letter at `x` into `into`; return its advance."""
    cmap = font.getBestCmap() or {}
    g = cmap.get(ord(ch))
    if g is None:
        return size * SPACE
    glyphs = font.getGlyphSet()
    scale = _scale(font, size)
    pen = _SkiaPen(glyphs)
    glyphs[g].draw(pen)
    m = skia.Matrix()
    m.setScale(scale, -scale)
    m.postTranslate(x, 0)
    p = skia.Path(pen.path)
    p.transform(m)
    into.addPath(p)
    return font["hmtx"][g][0] * scale  # pyright: ignore[reportIndexIssue]


def _scale(font: TTFont, size: float) -> float:
    """Px per font unit, for letters `size` px tall."""
    return size / font["head"].unitsPerEm  # pyright: ignore[reportAttributeAccessIssue]


def _metrics(font: TTFont, size: float) -> tuple[float, float]:
    """A label's ascent and descent, as shares of the font's own: the box
    its letters fill, tighter than the font's line."""
    scale = _scale(font, size)
    hhea = font["hhea"]
    return (
        hhea.ascent * scale * 0.72,  # pyright: ignore[reportAttributeAccessIssue]
        -hhea.descent * scale * 0.6,  # pyright: ignore[reportAttributeAccessIssue]
    )
