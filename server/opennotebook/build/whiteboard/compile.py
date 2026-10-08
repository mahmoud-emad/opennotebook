"""A scene as the pen draws it: every stroke and label placed on the board
and timed to the word that names it (docs/video-overview-spec.md, sections
4.4 to 4.6).

The scene JSON names grid cells; this places everything in pixels. An
element owns the box of its cells; its shape and label stay inside it, so
two elements in different cells cannot cross. Arrows join box edges. Labels
are shrunk to fit their box and wrapped onto two lines before they would
spill.

Timing follows the narration: an element starts as its word is said (a
beat of 120 ms early, as a hand starts as the word begins), draws at a
speed set by its length, and its label follows. Two elements on the same
word are drawn one after the other. When the drawing would not finish
before the scene ends, every piece is drawn proportionally faster.
"""

# skia-python ships without complete type information: its objects are
# unknown to the type checker, which is said once here rather than at every use.
# pyright: reportUnknownParameterType=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownArgumentType=false

import math
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

import skia

from opennotebook.build.whiteboard import geometry as g
from opennotebook.build.whiteboard import icons
from opennotebook.build.whiteboard import theme as th
from opennotebook.build.whiteboard.scene import COLS, ROWS, Beat, Element, Scene, cell

W, H = 1920, 1080
# The board: a title band at the top, then the 6×6 grid.
SAFE = (110.0, 175.0, 1810.0, 940.0)
TITLE_Y = 118.0
# An element's drawing keeps this far inside its cells.
PAD = 16.0

# The inks, the highlighter, the pen's width and the hand's sizes are the
# theme's (`theme.py`); what follows is how the board is laid out and timed.

# Drawing speed, px of line per ms, and the bounds of one element's time.
LINE_SPEED = 1.6
TEXT_SPEED = 0.9
SHAPE_MS = (300, 1400)
LABEL_MS = (250, 900)
LEAD_MS = 120
# A wash, a fill or a card is laid down in this long.
LAY_MS = 300.0
# The pen starts this long after the scene does, and however short the
# scene, its drawing is given at least `MIN_DRAW_MS`.
FIRST_MS = 100
MIN_DRAW_MS = 200
# The board is still for this long before a scene's end: the drawing is
# done before the next one starts.
SETTLE_MS = 500

# The smallest a label is set, px, before it is wrapped onto two lines.
MIN_LABEL = 30
# How much a label shrinks at each try to fit its box.
SHRINK = 0.92

# A label on its own is written larger than one inside a shape.
LABEL_SCALE = 1.2
# A box's label keeps this far inside its outline; a circle's, this share
# of its width.
BOX_INSET = 14.0
CIRCLE_INSET = 0.15
# A number: its figure's size, and the caption under it, in the ink.
FIGURE_SIZE = 120.0
FIGURE_CAPTION_SIZE = 40.0
FIGURE_CAPTION_H = 56.0
# The caption under a sketch or an icon, and the height kept for it.
CAPTION_SIZE = 44.0
SKETCH_CAPTION_H = 60.0
ICON_CAPTION_H = 62.0
# An icon fills this share of its room; on cut paper, its disc reaches this
# share of the icon's side from its centre.
ICON_SHARE = 0.86
DISC_SHARE = 0.62
# An icon's lines wobble a little less than a shape drawn freehand.
ICON_WOBBLE = 0.9
# The paper under an icon on cut paper, when the theme names no fill.
DISC_PAPER = (0xFF, 0xFF, 0xFF)
# An arrow leaves a box this far outside it; its label sits this far off
# its midpoint, in a box this size.
JOIN_GAP = 14.0
JOIN_LABEL_OFFSET = 30.0
JOIN_LABEL_BOX = (320.0, 64.0)
# A highlight's wash keeps this far inside the element's cells.
WASH_INSET = 6.0
# A highlight is drawn after everything else on its word.
HIGHLIGHT_ORDER = 10_000

type Box = tuple[float, float, float, float]
# Pieces drawn together: the ms they wait for, their place among those on
# the same ms, and the pieces.
type Group = tuple[float, int, list[Piece]]


@dataclass
class Piece:
    """One thing the pen does: a line drawn along its points, a label
    written left to right, a highlight washed in behind, a shape filled in
    (`shape`, a closed path) where a theme fills its boxes, or a card of
    paper laid under words on a picture."""

    element: str
    kind: Literal["line", "text", "wash", "fill", "card"]
    color: th.RGB
    start_ms: float = 0.0
    end_ms: float = 0.0
    points: g.Poly = field(default_factory=list[g.Point])
    text: g.Text | None = None
    # Where a label's baseline starts.
    at: g.Point = (0.0, 0.0)
    # What a label, a wash, a fill or a card covers.
    box: Box = (0.0, 0.0, 0.0, 0.0)
    width: float = field(default_factory=lambda: th.current().stroke)
    # A label's words, for what the lint says about it.
    said: str = ""
    shape: skia.Path | None = None


@dataclass
class Drawing:
    pieces: list[Piece]
    # Each element's box, for the lint.
    boxes: dict[str, Box]
    # What the compiler had to change or leave out, said plainly.
    notes: list[str]
    # The ids of arrows and lines.
    connectors: set[str] = field(default_factory=set[str])


def _seed(*parts: object) -> int:
    return zlib.crc32("|".join(map(str, parts)).encode())


def _is_join(e: Element) -> bool:
    return e.kind in ("arrow", "line")


def box_of(e: Element) -> Box:
    """The box of an element's cells, cut where its span runs off the grid."""
    assert e.at is not None
    col, row = cell(e.at)
    cols, rows = e.span
    cols, rows = min(cols, len(COLS) - col), min(rows, ROWS - row)
    x0, y0, x1, y1 = SAFE
    cw, ch = (x1 - x0) / len(COLS), (y1 - y0) / ROWS
    return (x0 + col * cw, y0 + row * ch, x0 + (col + cols) * cw, y0 + (row + rows) * ch)


def inset(b: Box, d: float) -> Box:
    """`b` shrunk by `d` on every side."""
    return (b[0] + d, b[1] + d, b[2] - d, b[3] - d)


def _centre(b: Box) -> g.Point:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def fit_text(s: str, max_w: float, size: float) -> list[g.Text]:
    """`s` at the largest size up to `size` that fits `max_w`, on one line,
    or on two when one would be smaller than `MIN_LABEL`."""
    while size >= MIN_LABEL:
        t = g.text(s, size)
        if t.width <= max_w:
            return [t]
        size *= SHRINK
    words = s.split()
    if len(words) < 2:
        return [g.text(s, MIN_LABEL * 0.8)]
    # Broken where the two lines come out nearest in length.
    half = min(range(1, len(words)), key=lambda i: abs(len(" ".join(words[:i])) - len(s) / 2))
    a, b = " ".join(words[:half]), " ".join(words[half:])
    size = MIN_LABEL * 1.4
    while size > MIN_LABEL * 0.8:
        ta, tb = g.text(a, size), g.text(b, size)
        if max(ta.width, tb.width) <= max_w:
            return [ta, tb]
        size *= SHRINK
    return [g.text(a, size), g.text(b, size)]


def _text_piece(eid: str, t: g.Text, at: g.Point, color: th.RGB, said: str) -> Piece:
    """A label written from `at`, on its baseline, boxed by its letters."""
    x, base = at
    box = (x, base - t.ascent, x + t.width, base + t.descent)
    return Piece(eid, "text", color, text=t, at=at, said=said, box=box)


def _label_pieces(eid: str, s: str, box: Box, color: th.RGB, size: float) -> list[Piece]:
    """A label centred in `box`, one piece per line."""
    if not s:
        return []
    x0, y0, x1, y1 = box
    lines = fit_text(s, x1 - x0, size)
    lh = max(t.ascent + t.descent for t in lines) * 1.05
    top = (y0 + y1) / 2 - lh * len(lines) / 2
    out: list[Piece] = []
    for i, t in enumerate(lines):
        base = top + lh * i + t.ascent
        out.append(_text_piece(eid, t, ((x0 + x1 - t.width) / 2, base), color, s))
    return out


def _edge(b: Box, toward: g.Point) -> g.Point:
    """Where the line from a box's centre toward a point leaves the box,
    a little outside it."""
    cx, cy = _centre(b)
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == 0 and dy == 0:
        return (cx, cy)
    hw, hh = (b[2] - b[0]) / 2 + JOIN_GAP, (b[3] - b[1]) / 2 + JOIN_GAP
    t = min(hw / abs(dx) if dx else math.inf, hh / abs(dy) if dy else math.inf)
    return (cx + dx * t, cy + dy * t)


def _strokes(eid: str, color: th.RGB, polys: list[g.Poly]) -> list[Piece]:
    return [Piece(eid, "line", color, points=p) for p in polys if len(p) > 1]


def _outlined(e: Element, inner: Box, color: th.RGB, polys: list[g.Poly]) -> list[Piece]:
    """A box's or circle's outline, and the theme's fill behind it."""
    look = th.current()
    out: list[Piece] = []
    if look.fill != "none" and polys and len(polys[0]) > 2:
        fill = look.fills.get(e.tone, color)
        out.append(Piece(e.id, "fill", fill, box=inner, shape=g.path_of(polys[0], closed=True)))
    if look.outline or not out:
        out += _strokes(e.id, color, polys)
    return out


def _caption(e: Element, inner: Box, height: float, color: th.RGB, size: float) -> list[Piece]:
    """An element's label in the foot of its box, `height` tall."""
    x0, _, x1, y1 = inner
    return _label_pieces(e.id, e.label, (x0, y1 - height, x1, y1), color, size)


def _sketch(e: Element, inner: Box, color: th.RGB, seed: int, notes: list[str]) -> list[Piece]:
    """A sketch's own path, fitted into its box above its label."""
    if not e.d:
        notes.append(f"sketch {e.id or '?'} has no path; left out")
        return []
    try:
        raw = g.svg_path([e.d])
    except Exception:
        notes.append(f"sketch {e.id or '?'} has a path that does not parse; left out")
        return []
    x0, y0, x1, y1 = inner
    w, h = x1 - x0, y1 - y0
    label_h = SKETCH_CAPTION_H if e.label else 0.0
    b = raw.getBounds()
    s = min(w / max(b.width(), 1), (h - label_h) / max(b.height(), 1))
    dx = x0 + (w - b.width() * s) / 2 - b.left() * s
    dy = y0 + (h - label_h - b.height() * s) / 2 - b.top() * s
    polys = [g.wobble(p, seed + i) for i, p in enumerate(g.polylines(g.transform(raw, s, dx, dy)))]
    return _strokes(e.id, color, polys) + _caption(e, inner, label_h, color, CAPTION_SIZE)


def _icon(e: Element, name: str, inner: Box, color: th.RGB, seed: int) -> list[Piece]:
    """An icon from the library, centred above its label; on cut paper, on a
    disc of paper of its own."""
    look = th.current()
    x0, y0, x1, y1 = inner
    w, h = x1 - x0, y1 - y0
    label_h = ICON_CAPTION_H if e.label else 0.0
    side = min(w, h - label_h) * ICON_SHARE
    s = side / icons.SIZE
    dx = (x0 + x1) / 2 - side / 2
    dy = y0 + (h - label_h - side) / 2
    polys = [
        g.wobble(p, seed + i, ICON_WOBBLE)
        for i, p in enumerate(g.polylines(g.transform(icons.path(name), s, dx, dy)))
    ]
    disc: list[Piece] = []
    if look.fill == "solid":
        cx, cy, r = dx + side / 2, dy + side / 2, side * DISC_SHARE
        ring = skia.Path()
        ring.addCircle(cx, cy, r)
        paper = look.fills.get("ink", DISC_PAPER)
        disc = [Piece(e.id, "fill", paper, box=(cx - r, cy - r, cx + r, cy + r), shape=ring)]
    caption = _caption(e, inner, label_h, color, CAPTION_SIZE)
    return disc + _strokes(e.id, color, polys) + caption


def shape(e: Element, box: Box, notes: list[str]) -> list[Piece]:
    """An element's own strokes and labels, untimed."""
    look = th.current()
    color = look.ink[e.tone]
    size = look.label_size
    seed = _seed(e.id, e.kind, e.at, e.label)
    inner = inset(box, PAD)
    x0, y0, x1, y1 = inner
    w, h = x1 - x0, y1 - y0

    if e.kind == "box":
        out = _outlined(e, inner, color, g.rough_rect(x0, y0, w, h, seed))
        return out + _label_pieces(e.id, e.label, inset(inner, BOX_INSET), color, size)
    if e.kind == "circle":
        ellipse = g.rough_ellipse((x0 + x1) / 2, (y0 + y1) / 2, w / 2, h / 2, seed)
        out = _outlined(e, inner, color, ellipse)
        label_box = inset(inner, w * CIRCLE_INSET)
        return out + _label_pieces(e.id, e.label, label_box, color, size)
    if e.kind == "label":
        return _label_pieces(e.id, e.label or e.text, inner, color, size * LABEL_SCALE)
    if e.kind == "number":
        label_h = FIGURE_CAPTION_H if e.label else 0.0
        figure_box = (x0, y0, x1, y1 - label_h)
        fig = _label_pieces(e.id, e.text or e.label, figure_box, color, FIGURE_SIZE)
        if not e.text:
            return fig
        return fig + _caption(e, inner, label_h, look.ink["ink"], FIGURE_CAPTION_SIZE)
    if e.kind == "sketch":
        return _sketch(e, inner, color, seed, notes)
    # An icon, or what stands in for one the library does not have.
    name = e.icon or ""
    if not icons.exists(name):
        found = icons.search(f"{name} {e.label}", 1)
        if not found:
            notes.append(f"icon {name!r} is not in the library and nothing like it is; drew a box")
            return shape(e.model_copy(update={"kind": "box"}), box, notes)
        notes.append(f"icon {name!r} is not in the library; drew {found[0]!r}")
        name = found[0]
    return _icon(e, name, inner, color, seed)


# The shortest an arrow is drawn. Two joined things in neighbouring cells
# leave almost no room between them, and a model puts them there however it
# is told not to. Such an arrow is drawn this long about its midpoint,
# reaching a little into both ends, rather than the scene being refused.
MIN_JOIN = 70.0


def _at_least(p: g.Point, q: g.Point, length: float) -> tuple[g.Point, g.Point]:
    """The ends of a join, moved apart about its midpoint to `length` when
    they are nearer than that."""
    d = math.dist(p, q)
    if d >= length:
        return p, q
    mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
    if d < 1e-6:
        # Ends on one point: no direction to keep, so left to right.
        ux, uy = 1.0, 0.0
    else:
        ux, uy = (q[0] - p[0]) / d, (q[1] - p[1]) / d
    h = length / 2
    return (mx - ux * h, my - uy * h), (mx + ux * h, my + uy * h)


def connector(e: Element, boxes: dict[str, Box], notes: list[str]) -> list[Piece]:
    """An arrow or line between two drawn elements, and its label beside it."""
    a, b = boxes.get(e.source or ""), boxes.get(e.target or "")
    if a is None or b is None:
        notes.append(
            f"{e.kind} {e.id or '?'} joins {e.source!r} to {e.target!r}, which "
            "are not both in the scene; left out"
        )
        return []
    if e.source == e.target:
        notes.append(f"{e.kind} {e.id} joins {e.source!r} to itself; left out")
        return []
    p, q = _at_least(_edge(a, _centre(b)), _edge(b, _centre(a)), MIN_JOIN)
    polys = g.arrow(p, q, _seed(e.id, e.source, e.target), head=e.kind == "arrow")
    color = th.current().ink[e.tone]
    out = [Piece(e.id, "line", color, points=pl) for pl in polys]
    if e.label:
        mx, my = (p[0] + q[0]) / 2, (p[1] + q[1]) / 2
        n = math.hypot(q[0] - p[0], q[1] - p[1]) or 1.0
        # Opposite the side the curve bends to, so the line does not run
        # through its own label.
        ox = (q[1] - p[1]) / n * JOIN_LABEL_OFFSET
        oy = -(q[0] - p[0]) / n * JOIN_LABEL_OFFSET
        hw, hh = JOIN_LABEL_BOX[0] / 2, JOIN_LABEL_BOX[1] / 2
        label_box = (mx + ox - hw, my + oy - hh, mx + ox + hw, my + oy + hh)
        out += _label_pieces(e.id, e.label, label_box, color, CAPTION_SIZE)
    return out


def _clamp(v: float, lo_hi: tuple[int, int]) -> float:
    return min(max(v, lo_hi[0]), lo_hi[1])


def _duration(p: Piece) -> float:
    """How long a piece takes at the pen's own speed."""
    if p.kind == "line":
        return g.length_of(p.points) / LINE_SPEED
    if p.kind == "text" and p.text is not None:
        return p.text.width / TEXT_SPEED
    return LAY_MS


def compile_scene(
    sc: Scene,
    when: Callable[[Beat], float],
    start_ms: float,
    end_ms: float,
    theme: th.Theme | str | None = None,
) -> Drawing:
    """The scene placed and timed, in `theme` (or the one in use). `when`
    turns a beat into the ms its word is said; the scene is on screen from
    `start_ms` to `end_ms`."""
    if theme is not None:
        with th.using(theme):
            return compile_scene(sc, when, start_ms, end_ms)
    look = th.current()
    notes: list[str] = []
    # An element the model left unnamed is named by its place.
    elements = [
        e if e.id else e.model_copy(update={"id": f"_e{i}"}) for i, e in enumerate(sc.elements)
    ]
    boxes: dict[str, Box] = {}
    content: dict[str, Box] = {}
    groups: list[Group] = []
    placed = [e for e in elements if not _is_join(e)]
    for i, e in enumerate(placed):
        if e.at is None:
            notes.append(f"{e.kind} {e.id} has no cell; left out")
            continue
        box = box_of(e)
        boxes[e.id] = box
        pieces = shape(e, box, notes)
        if pieces:
            # What an arrow joins: the drawing, which sits well inside its
            # cells, not the cells, which may touch their neighbour's.
            content[e.id] = union([extent(p) for p in pieces])
        groups.append((when(e.beat), i, pieces))
    for i, e in enumerate(elements):
        if _is_join(e):
            groups.append((when(e.beat), len(placed) + i, connector(e, content, notes)))
    for h in sc.highlight:
        b = boxes.get(h.target)
        if b is None:
            notes.append(f"highlight of {h.target!r}, which is not in the scene; left out")
            continue
        wash = Piece(h.target, "wash", look.highlight, box=inset(b, WASH_INSET))
        groups.append((when(h.beat), HIGHLIGHT_ORDER, [wash]))
    title: list[Piece] = []
    if sc.title:
        t = fit_text(sc.title, SAFE[2] - SAFE[0], look.title_size)[0]
        title = [_text_piece("title", t, (SAFE[0], TITLE_Y), look.ink["ink"], sc.title)]
    timed = _schedule(title, groups, start_ms, end_ms)
    for p in timed:
        if p.text is not None and p.text.missing:
            notes.append(
                f"the label {p.said!r} has letters the board cannot write yet "
                f"({''.join(p.text.missing)}); drawn as ?"
            )
    joins = {e.id for e in elements if _is_join(e)}
    return Drawing(timed, boxes, notes, joins)


# ── an illustrated scene: its labels on cards over its picture ───────────────

# The band the cards sit in, where the picture was asked to stay calm.
CARD_BAND = (760.0, 1010.0)
CARD_PAD = (20.0, 12.0)
CARD_GAP = 14.0
# The sizes a card's words are tried at, largest first.
CARD_SIZES = (40.0, 34.0, 28.0)
# A card's line, as a share of its words' size.
CARD_LINE = 1.15
# The title's card: its top, and its words' share of the theme's title size.
CARD_TITLE_Y = 64.0
CARD_TITLE_SCALE = 0.85
# The widest a card's words may be.
CARD_TEXT_W = SAFE[2] - SAFE[0] - 2 * CARD_PAD[0]

type CardRow = list[tuple[Element, str, g.Text]]


def _card_texts(sc: Scene) -> list[tuple[Element, str]]:
    """What an illustrated scene writes: each element's words, and each arrow
    as the relation it asserts, in the order they are said."""
    names = {e.id: (e.label or e.text or "") for e in sc.elements if e.id}
    out: list[tuple[Element, str]] = []
    for e in sc.elements:
        if _is_join(e):
            a, b = names.get(e.source or "", ""), names.get(e.target or "", "")
            if a and b:
                verb = f" {e.label} " if e.label else (" → " if e.kind == "arrow" else " — ")
                out.append((e, f"{a}{verb}{b}"))
        elif e.kind == "number" and e.text:
            out.append((e, f"{e.text} {e.label}".strip()))
        elif e.label or e.text:
            out.append((e, e.label or e.text))
    return out


def _card_w(t: g.Text) -> float:
    return t.width + 2 * CARD_PAD[0]


def _card_h(size: float) -> float:
    return size * CARD_LINE + 2 * CARD_PAD[1]


def _wrap_cards(texts: list[tuple[Element, str]], size: float) -> list[CardRow]:
    """The cards at `size`, in rows as wide as the board allows."""
    rows: list[CardRow] = [[]]
    width = 0.0
    for e, s in texts:
        t = fit_text(s, CARD_TEXT_W, size)[0]
        w = _card_w(t)
        if rows[-1] and width + CARD_GAP + w > SAFE[2] - SAFE[0]:
            rows.append([])
            width = 0.0
        rows[-1].append((e, s, t))
        width += (CARD_GAP if width else 0) + w
    return rows


def _card_rows(texts: list[tuple[Element, str]]) -> tuple[list[CardRow], float]:
    """The cards in rows at the largest size whose rows fit the band, or
    the smallest size when none does."""
    for size in CARD_SIZES[:-1]:
        rows = _wrap_cards(texts, size)
        if len(rows) * (_card_h(size) + CARD_GAP) <= CARD_BAND[1] - CARD_BAND[0]:
            return rows, size
    return _wrap_cards(texts, CARD_SIZES[-1]), CARD_SIZES[-1]


def _title_card(title: str) -> list[Piece]:
    """An illustrated scene's title, on a card at the top of the board."""
    look = th.current()
    t = fit_text(title, CARD_TEXT_W, look.title_size * CARD_TITLE_SCALE)[0]
    x0, y0 = SAFE[0] - CARD_PAD[0], CARD_TITLE_Y
    h = t.ascent + t.descent + 2 * CARD_PAD[1]
    words = _text_piece("title", t, (SAFE[0], y0 + CARD_PAD[1] + t.ascent), look.ink["ink"], title)
    # The words' box is the card's height, for the card is what covers them.
    words.box = (SAFE[0], y0, SAFE[0] + t.width, y0 + h)
    return [Piece("title", "card", look.paper, box=(x0, y0, x0 + _card_w(t), y0 + h)), words]


def compile_illustrated(
    sc: Scene, when: Callable[[Beat], float], start_ms: float, end_ms: float
) -> Drawing:
    """An illustrated scene placed and timed: the picture is the board; the
    scene's own words are cards in the calm band at its foot, each laid down
    on the word that names it, and its title on a card at the top."""
    look = th.current()
    rows, size = _card_rows(_card_texts(sc))
    line_h = _card_h(size)
    top = CARD_BAND[1] - len(rows) * (line_h + CARD_GAP)
    groups: list[Group] = []
    k = 0
    for r, row in enumerate(rows):
        widths = [_card_w(t) for _, _, t in row]
        x = (SAFE[0] + SAFE[2]) / 2 - (sum(widths) + CARD_GAP * (len(row) - 1)) / 2
        y = top + r * (line_h + CARD_GAP)
        for (e, said, t), w in zip(row, widths, strict=True):
            eid = e.id or f"_c{k}"
            base = y + CARD_PAD[1] + (size * CARD_LINE - (t.ascent + t.descent)) / 2 + t.ascent
            card = Piece(eid, "card", look.paper, box=(x, y, x + w, y + line_h))
            words = _text_piece(eid, t, (x + CARD_PAD[0], base), look.ink[e.tone], said)
            groups.append((when(e.beat), k, [card, words]))
            x += w + CARD_GAP
            k += 1
    title = _title_card(sc.title) if sc.title else []
    return Drawing(_schedule(title, groups, start_ms, end_ms), {}, [], set())


def _schedule(
    title: list[Piece], groups: list[Group], start_ms: float, end_ms: float
) -> list[Piece]:
    """Give every piece its time, one at a time, done `SETTLE_MS` before the
    scene ends: the title first, then each group in the order its word is
    said.

    A piece starts on its word, or as soon as the one before it is done;
    but never later than leaves time for every piece after it, so a scene
    whose last words come at its very end draws them a little early rather
    than late. Only when the drawing is longer than the whole scene is the
    pen faster, by just enough."""
    limit = max(end_ms - SETTLE_MS, start_ms + MIN_DRAW_MS)
    order: list[tuple[float | None, Piece, float]] = []
    for p in title:
        order.append((None, p, _clamp(_duration(p), LABEL_MS)))
    for at, _, pieces in sorted(groups, key=lambda x: (x[0], x[1])):
        shape_ms = sum(_duration(p) for p in pieces if p.kind == "line")
        share = _clamp(shape_ms, SHAPE_MS) / shape_ms if shape_ms else 1.0
        for k, p in enumerate(pieces):
            if p.kind == "line":
                d = _duration(p) * share
            elif p.kind == "text":
                d = _clamp(_duration(p), LABEL_MS)
            else:
                d = _duration(p)
            # Only a group's first piece waits for its word; the rest follow it.
            order.append((max(at - LEAD_MS, start_ms) if k == 0 else None, p, d))
    first = start_ms + FIRST_MS
    need = sum(d for _, _, d in order)
    speed = max(need / max(limit - first, 1.0), 1.0)
    # The latest each piece may start: what is left after it must still fit.
    after = 0.0
    latest: list[float] = []
    for _, _, d in reversed(order):
        after += d / speed
        latest.append(limit - after)
    latest.reverse()
    cursor = first
    out: list[Piece] = []
    for (word, p, d), last in zip(order, latest, strict=True):
        begin = cursor if word is None else max(cursor, word)
        begin = max(min(begin, last), cursor)
        p.start_ms, p.end_ms = begin, begin + d / speed
        cursor = p.end_ms
        out.append(p)
    return out


def extent(p: Piece) -> Box:
    """What a piece covers: a line's points, or its box."""
    if p.kind == "line" and p.points:
        xs, ys = [x for x, _ in p.points], [y for _, y in p.points]
        return (min(xs), min(ys), max(xs), max(ys))
    return p.box


def union(bs: list[Box]) -> Box:
    """The smallest box that holds every one of `bs`."""
    return (
        min(b[0] for b in bs),
        min(b[1] for b in bs),
        max(b[2] for b in bs),
        max(b[3] for b in bs),
    )
